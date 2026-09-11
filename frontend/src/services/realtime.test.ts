import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  CLOSE,
  REORDER_WINDOW,
  RESYNC,
  REVOKED,
  RealtimeClient,
  RealtimeEvent,
  websocketUrl,
} from './realtime';

/**
 * A controllable stand-in for the browser's WebSocket.
 *
 * There is no server in a unit test, so the test plays the server: it
 * decides when frames arrive and when the connection closes. What is under
 * test is the client's behaviour in response -- the protocol itself is
 * verified end to end against the real endpoint in the backend suite.
 */
class FakeSocket {
  static instances: FakeSocket[] = [];
  readyState = 1;
  sent: Record<string, unknown>[] = [];
  closedWith: [number, string] | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: { code: number; reason: string }) => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(
    public url: string,
    public protocols: string[]
  ) {
    FakeSocket.instances.push(this);
  }

  send(data: string) {
    this.sent.push(JSON.parse(data));
  }

  close(code = 1000, reason = '') {
    this.closedWith = [code, reason];
    this.readyState = 3;
  }

  // --- the server's side ---
  deliver(frame: Partial<RealtimeEvent> & { type: string }) {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }

  ready(data: Record<string, unknown> = {}) {
    this.deliver({
      type: 'connection.ready',
      data: { seq: 10, epoch: 'E1', online: ['me'], heartbeatSeconds: 10, ...data },
    });
  }

  event(seq: number, type = 'document.status', data: Record<string, unknown> = {}, epoch = 'E1') {
    this.deliver({ type, seq, epoch, workspace_id: 'ws', data, ts: '', actor_id: null });
  }

  drop(code: number) {
    this.readyState = 3;
    this.onclose?.({ code, reason: '' });
  }
}

const latest = () => FakeSocket.instances[FakeSocket.instances.length - 1];

function build(overrides: Partial<ConstructorParameters<typeof RealtimeClient>[0]> = {}) {
  const client = new RealtimeClient({
    createSocket: (url, protocols) => new FakeSocket(url, protocols) as unknown as WebSocket,
    getToken: () => 'token-1',
    refreshToken: async () => false,
    origin: () => 'ws://app.test',
    random: () => 0,
    now: () => 1_000,
    ...overrides,
  });
  const events: RealtimeEvent[] = [];
  client.subscribe((e) => events.push(e));
  return { client, events };
}

const types = (events: RealtimeEvent[]) => events.map((e) => e.type);

beforeEach(() => {
  FakeSocket.instances = [];
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('connecting', () => {
  it('offers the token as a subprotocol and never puts it in the URL', () => {
    const { client } = build();
    client.connect('ws-1');
    const socket = latest();
    expect(socket.protocols).toEqual(['bearer', 'token-1']);
    expect(socket.url).toBe('ws://app.test/api/v1/ws?workspace_id=ws-1');
    expect(socket.url).not.toContain('token');
  });

  it('does not connect without a token', () => {
    const { client } = build({ getToken: () => null });
    client.connect('ws-1');
    expect(FakeSocket.instances).toHaveLength(0);
    expect(client.status).toBe('unauthorized');
  });

  it('is open once ready, and asks screens to load fresh state', () => {
    const { client, events } = build();
    client.connect('ws-1');
    latest().ready();
    expect(client.status).toBe('open');
    expect(client.online).toEqual(['me']);
    expect(types(events)).toEqual([RESYNC]);
    expect(events[0].data).toEqual({ reason: 'initial' });
  });

  it('switching workspace opens a new socket in a new sequence space', () => {
    const { client, events } = build();
    client.connect('ws-1');
    latest().ready();
    const first = latest();
    client.connect('ws-2');
    expect(first.closedWith?.[0]).toBe(1000);
    expect(latest().url).toContain('workspace_id=ws-2');
    latest().ready();
    // No resume across workspaces: the second is a fresh start.
    expect(latest().sent.find((f) => f.type === 'resume')).toBeUndefined();
    expect(types(events)).toEqual([RESYNC, RESYNC]);
  });

  it('builds a secure URL on an https page', () => {
    expect(websocketUrl('w', 'wss://secure.example')).toBe(
      'wss://secure.example/api/v1/ws?workspace_id=w'
    );
  });
});

describe('delivery', () => {
  it('delivers events in the order they arrive', () => {
    const { client, events } = build();
    client.connect('ws-1');
    latest().ready();
    latest().event(11, 'document.created', { id: 'a' });
    latest().event(12, 'document.status', { id: 'a', progress: 35 });
    expect(types(events).slice(1)).toEqual(['document.created', 'document.status']);
  });

  it('drops an exact duplicate', () => {
    const { client, events } = build();
    client.connect('ws-1');
    latest().ready();
    latest().event(11);
    latest().event(11);
    expect(events.filter((e) => e.seq === 11)).toHaveLength(1);
    expect(client.stats.duplicates).toBe(1);
  });

  it('keeps an event that arrives out of order', () => {
    // Published close together on two servers, delivered crosswise. A
    // high-water-mark dedupe would drop 12 here and lose it.
    const { client, events } = build();
    client.connect('ws-1');
    latest().ready();
    latest().event(13);
    latest().event(12);
    expect(events.map((e) => e.seq).slice(1)).toEqual([13, 12]);
    expect(client.stats.duplicates).toBe(0);
  });

  it('ignores events from before this client started watching', () => {
    const { client, events } = build();
    client.connect('ws-1');
    latest().ready({ seq: 10 });
    latest().event(9);
    expect(events.map((e) => e.seq)).toEqual([0]); // just the resync
  });

  it('adopts a new epoch mid-stream and asks for a resync', () => {
    const { client, events } = build();
    client.connect('ws-1');
    latest().ready({ seq: 40 });
    // The counter restarted: seq 1 in a new epoch must not look like a repeat.
    latest().event(1, 'document.created', { id: 'x' }, 'E2');
    expect(types(events)).toEqual([RESYNC, RESYNC, 'document.created']);
    expect(events[1].data).toEqual({ reason: 'epoch-changed' });
  });

  it('keeps presence current', () => {
    const { client } = build();
    const seen: string[][] = [];
    client.onPresence((online) => seen.push(online));
    client.connect('ws-1');
    latest().ready({ online: ['me'] });
    latest().event(11, 'presence.online', { userId: 'you', online: ['me', 'you'] });
    latest().event(12, 'presence.offline', { userId: 'you', online: ['me'] });
    expect(client.online).toEqual(['me']);
    expect(seen).toEqual([['me'], ['me', 'you'], ['me']]);
  });

  it('one failing listener does not stop the others', () => {
    const { client } = build();
    const heard: string[] = [];
    client.subscribe(() => {
      throw new Error('a buggy screen');
    });
    client.subscribe((e) => heard.push(e.type));
    client.connect('ws-1');
    latest().ready();
    latest().event(11);
    expect(heard).toContain('document.status');
  });

  it('an unsubscribed listener hears nothing more', () => {
    const { client } = build();
    const heard: string[] = [];
    const stop = client.subscribe((e) => heard.push(e.type));
    client.connect('ws-1');
    latest().ready();
    stop();
    latest().event(11);
    expect(heard).toEqual([RESYNC]);
  });
});

describe('reconnecting', () => {
  it('reconnects after an unexpected close and resumes where it was', () => {
    const { client } = build();
    client.connect('ws-1');
    latest().ready({ seq: 10 });
    latest().event(11);
    latest().event(12);
    latest().drop(1006);
    expect(client.status).toBe('reconnecting');

    vi.advanceTimersByTime(250);
    expect(FakeSocket.instances).toHaveLength(2);
    latest().ready({ seq: 15 });
    const resume = latest().sent.find((f) => f.type === 'resume');
    // From a little behind the highest seen, never before the baseline.
    expect(resume).toEqual({ type: 'resume', after: Math.max(10, 12 - REORDER_WINDOW), epoch: 'E1' });
  });

  it('a complete resume needs no refetch; an incomplete one does', () => {
    const { client, events } = build();
    client.connect('ws-1');
    latest().ready();
    latest().drop(1006);
    vi.advanceTimersByTime(250);
    latest().ready();
    latest().deliver({ type: 'connection.resumed', data: { complete: true, seq: 10, epoch: 'E1' } });
    expect(types(events)).toEqual([RESYNC]);

    latest().drop(1006);
    vi.advanceTimersByTime(250);
    latest().ready();
    latest().deliver({ type: 'connection.resumed', data: { complete: false, seq: 99, epoch: 'E1' } });
    expect(types(events)).toEqual([RESYNC, RESYNC]);
    expect(events[1].data).toEqual({ reason: 'incomplete' });
  });

  it('a replayed event already seen live is dropped', () => {
    const { client, events } = build();
    client.connect('ws-1');
    latest().ready({ seq: 10 });
    latest().event(11, 'document.created', { id: 'a' });
    latest().drop(1006);
    vi.advanceTimersByTime(250);
    latest().ready({ seq: 12 });
    latest().event(11, 'document.created', { id: 'a' });
    latest().event(12, 'document.created', { id: 'b' });
    expect(events.filter((e) => e.type === 'document.created').map((e) => e.data)).toEqual([
      { id: 'a' },
      { id: 'b' },
    ]);
  });

  it('after a restart (new epoch) it refetches instead of resuming', () => {
    const { client, events } = build();
    client.connect('ws-1');
    latest().ready({ epoch: 'E1' });
    latest().drop(1001);
    vi.advanceTimersByTime(250);
    latest().ready({ epoch: 'E2', seq: 3 });
    expect(latest().sent.find((f) => f.type === 'resume')).toBeUndefined();
    expect(events[1].data).toEqual({ reason: 'epoch-changed' });
  });

  it('backs off exponentially, with a ceiling', () => {
    const { client } = build();
    client.connect('ws-1');
    // random() = 0 puts each delay at half its ceiling: 250, 500, 1000...
    const expected = [250, 500, 1000, 2000, 4000, 8000, 15000, 15000];
    for (const delay of expected) {
      latest().drop(1006);
      const before = FakeSocket.instances.length;
      vi.advanceTimersByTime(delay - 1);
      expect(FakeSocket.instances).toHaveLength(before);
      vi.advanceTimersByTime(1);
      expect(FakeSocket.instances).toHaveLength(before + 1);
    }
  });

  it('a successful connection resets the backoff', () => {
    const { client } = build();
    client.connect('ws-1');
    latest().drop(1006);
    vi.advanceTimersByTime(250);
    latest().drop(1006);
    vi.advanceTimersByTime(500);
    latest().ready();
    latest().drop(1006);
    const before = FakeSocket.instances.length;
    vi.advanceTimersByTime(250);
    expect(FakeSocket.instances).toHaveLength(before + 1);
  });

  it('skips the remaining backoff when the network returns', () => {
    const { client } = build();
    client.connect('ws-1');
    latest().drop(1006);
    expect(client.status).toBe('reconnecting');
    const before = FakeSocket.instances.length;
    client.reconnectNow();
    expect(FakeSocket.instances).toHaveLength(before + 1);
  });

  it('too many connections waits longer before trying again', () => {
    const { client } = build();
    client.connect('ws-1');
    latest().drop(CLOSE.TOO_MANY);
    const before = FakeSocket.instances.length;
    vi.advanceTimersByTime(9_999);
    expect(FakeSocket.instances).toHaveLength(before);
    vi.advanceTimersByTime(1);
    expect(FakeSocket.instances).toHaveLength(before + 1);
  });
});

describe('authentication and access', () => {
  it('refreshes the token once on a refusal, then reconnects', async () => {
    let token = 'expired';
    const refreshToken = vi.fn(async () => {
      token = 'fresh';
      return true;
    });
    const { client } = build({ getToken: () => token, refreshToken });
    client.connect('ws-1');
    latest().drop(CLOSE.UNAUTHORIZED);
    await vi.waitFor(() => expect(FakeSocket.instances).toHaveLength(2));
    expect(refreshToken).toHaveBeenCalledOnce();
    expect(latest().protocols).toEqual(['bearer', 'fresh']);
  });

  it('gives up if the refreshed token is refused too', async () => {
    const refreshToken = vi.fn(async () => true);
    const { client } = build({ refreshToken });
    client.connect('ws-1');
    latest().drop(CLOSE.UNAUTHORIZED);
    await vi.waitFor(() => expect(FakeSocket.instances).toHaveLength(2));
    latest().drop(CLOSE.UNAUTHORIZED);
    vi.advanceTimersByTime(60_000);
    expect(FakeSocket.instances).toHaveLength(2);
    expect(client.status).toBe('unauthorized');
    expect(refreshToken).toHaveBeenCalledOnce();
  });

  it('stops, and says why, when access is withdrawn', () => {
    const { client, events } = build();
    client.connect('ws-1');
    latest().ready();
    latest().drop(CLOSE.FORBIDDEN);
    vi.advanceTimersByTime(60_000);
    expect(FakeSocket.instances).toHaveLength(1);
    expect(client.status).toBe('forbidden');
    expect(types(events)).toContain(REVOKED);
  });
});

describe('heartbeat', () => {
  it('pings at the interval the server advertised', () => {
    const { client } = build();
    client.connect('ws-1');
    latest().ready({ heartbeatSeconds: 10 });
    vi.advanceTimersByTime(10_000);
    expect(latest().sent).toContainEqual({ type: 'ping', t: 1_000 });
  });

  it('abandons a connection that has gone silent and reconnects', () => {
    const { client } = build();
    client.connect('ws-1');
    latest().ready({ heartbeatSeconds: 10 });
    const silent = latest();
    // Two missed intervals plus slack, with nothing at all from the server.
    vi.advanceTimersByTime(25_000);
    expect(silent.closedWith?.[0]).toBe(CLOSE.HEARTBEAT_LOST);
    expect(client.status).toBe('reconnecting');
    vi.advanceTimersByTime(250);
    expect(FakeSocket.instances).toHaveLength(2);
  });

  it('any frame from the server counts as a sign of life', () => {
    const { client } = build();
    client.connect('ws-1');
    latest().ready({ heartbeatSeconds: 10 });
    vi.advanceTimersByTime(20_000);
    latest().deliver({ type: 'connection.pong', data: {} });
    vi.advanceTimersByTime(20_000);
    expect(latest().closedWith).toBeNull();
    expect(client.status).toBe('open');
  });
});

describe('disconnecting', () => {
  it('closes cleanly and does not come back', () => {
    const { client } = build();
    client.connect('ws-1');
    latest().ready();
    const socket = latest();
    client.disconnect();
    expect(socket.closedWith?.[0]).toBe(1000);
    expect(client.status).toBe('idle');
    socket.drop(1000);
    vi.advanceTimersByTime(60_000);
    expect(FakeSocket.instances).toHaveLength(1);
  });

  it('a close from an abandoned socket is ignored', () => {
    const { client } = build();
    client.connect('ws-1');
    const old = latest();
    client.connect('ws-2');
    old.drop(1006);
    expect(client.status).not.toBe('reconnecting');
  });
});
