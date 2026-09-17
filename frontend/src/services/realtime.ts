/**
 * Live workspace events over a WebSocket.
 *
 * One connection per active workspace, replacing the 2.5-second status
 * polling. The REST API stays the source of truth: this client only learns
 * that something happened, and tells the screens that care.
 *
 * What it is responsible for, and why each part exists:
 *
 *  - **Authentication in the subprotocol.** The token is offered as
 *    `["bearer", token]`, never put in the URL, where every access log and
 *    proxy log would record it. A 4401 close triggers one silent refresh.
 *  - **Reconnect with exponential backoff and jitter**, so a server restart
 *    does not have every browser reconnect in the same instant.
 *  - **A heartbeat in both directions.** The client pings; the server's pong
 *    is how the client notices a connection that died without closing --
 *    a laptop lid, a NAT timeout -- instead of trusting a socket that will
 *    never deliver again.
 *  - **Resume.** A reconnecting client asks for what it missed. The server
 *    answers with whether that was everything; when it was not, or when the
 *    sequence restarted (a new epoch), the client emits a resync and the
 *    screens refetch over REST instead of showing a partial picture.
 *  - **Duplicate suppression by identity, not by high-water mark.** Events
 *    are sequenced when published but delivered as they arrive, so two
 *    published close together on different servers can arrive out of order.
 *    Dropping everything at or below the highest sequence seen would lose the
 *    earlier one; dropping exact repeats loses nothing.
 */
import { API_BASE, refreshSession, tokenStore } from './apiClient';

export type RealtimeStatus =
  | 'idle'
  | 'connecting'
  | 'open'
  | 'reconnecting'
  /** The token was refused and could not be refreshed. */
  | 'unauthorized'
  /** Not a member, a disallowed origin, or membership was revoked. */
  | 'forbidden';

export interface RealtimeEvent<T = Record<string, unknown>> {
  type: string;
  workspace_id: string;
  data: T;
  seq: number;
  epoch: string;
  ts: string;
  actor_id: string | null;
}

/**
 * Emitted by this client, never by the server, when local state can no longer
 * be assumed complete: on first connect, after a resume the server reported
 * as incomplete, and when the sequence restarted. Screens refetch on it.
 */
export const RESYNC = 'realtime.resync';
/** Emitted when the server closes the socket because access was withdrawn. */
export const REVOKED = 'realtime.revoked';

/** Server event names; mirrors EventType in app/realtime/events.py. */
export const EVENTS = {
  DOCUMENT_CREATED: 'document.created',
  DOCUMENT_STATUS: 'document.status',
  DOCUMENT_DELETED: 'document.deleted',
  MEMBER_ADDED: 'member.added',
  MEMBER_REMOVED: 'member.removed',
  MEMBER_ROLE_CHANGED: 'member.role_changed',
  INVITATION_SENT: 'invitation.sent',
  INVITATION_REVOKED: 'invitation.revoked',
  INVITATION_ACCEPTED: 'invitation.accepted',
  CHAT_CREATED: 'chat.created',
  CHAT_RENAMED: 'chat.renamed',
  CHAT_DELETED: 'chat.deleted',
  CHAT_MESSAGE: 'chat.message',
  PRESENCE_ONLINE: 'presence.online',
  PRESENCE_OFFLINE: 'presence.offline',
} as const;

/** Close codes the server uses; see app/realtime/manager.py. */
export const CLOSE = {
  UNAUTHORIZED: 4401,
  FORBIDDEN: 4403,
  TOO_MANY: 4429,
  HEARTBEAT_LOST: 4000,
} as const;

const OPEN = 1;
const BACKOFF_BASE_MS = 500;
const BACKOFF_MAX_MS = 30_000;
/** Too many connections is not fixed by trying again quickly. */
const TOO_MANY_MIN_DELAY_MS = 10_000;
const DEFAULT_HEARTBEAT_SECONDS = 25;
/**
 * How far behind the highest sequence seen a reconnecting client asks to be
 * replayed from. Out-of-order arrival spans a handful of events at most; a
 * margin this size, with exact duplicates dropped, means a disconnect in the
 * middle of a reordered pair loses neither.
 */
export const REORDER_WINDOW = 50;
/** Sequences remembered for duplicate suppression. */
const SEEN_LIMIT = 1000;

export interface RealtimeOptions {
  createSocket?: (url: string, protocols: string[]) => WebSocket;
  getToken?: () => string | null;
  refreshToken?: () => Promise<boolean>;
  /** `ws://host` or `wss://host`; defaults to the page's own origin. */
  origin?: () => string;
  random?: () => number;
  now?: () => number;
}

/** The socket URL for a workspace. Carries no credential. */
export function websocketUrl(workspaceId: string, origin?: string): string {
  const base =
    origin ?? `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}`;
  return `${base}${API_BASE}/ws?workspace_id=${encodeURIComponent(workspaceId)}`;
}

type Listener = (event: RealtimeEvent) => void;

export class RealtimeClient {
  status: RealtimeStatus = 'idle';
  online: string[] = [];
  readonly stats = { duplicates: 0, reconnects: 0, resyncs: 0 };

  private readonly deps: Required<RealtimeOptions>;
  private socket: WebSocket | null = null;
  private workspaceId: string | null = null;
  private stopped = true;

  // Sequence state for the current workspace.
  private epoch: string | null = null;
  /** Position when this client's view began; older events are already covered. */
  private baseline = 0;
  private highest = 0;
  private seen = new Set<number>();
  private seenOrder: number[] = [];

  private attempts = 0;
  private refreshedAfterRefusal = false;
  private heartbeatMs = DEFAULT_HEARTBEAT_SECONDS * 1000;
  private reconnectTimer: ReturnType<typeof setTimeout> | undefined;
  private heartbeatTimer: ReturnType<typeof setInterval> | undefined;
  private deadlineTimer: ReturnType<typeof setTimeout> | undefined;

  private readonly listeners = new Set<Listener>();
  private readonly statusListeners = new Set<(status: RealtimeStatus) => void>();
  private readonly presenceListeners = new Set<(online: string[]) => void>();

  constructor(options: RealtimeOptions = {}) {
    this.deps = {
      createSocket: options.createSocket ?? ((url, protocols) => new WebSocket(url, protocols)),
      getToken: options.getToken ?? (() => tokenStore.getAccess()),
      refreshToken: options.refreshToken ?? refreshSession,
      origin:
        options.origin ??
        (() =>
          `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}`),
      random: options.random ?? Math.random,
      now: options.now ?? Date.now,
    };
  }

  // ---------------------------------------------------------- public API --
  /** Open (or switch) the connection to a workspace. */
  connect(workspaceId: string): void {
    if (this.workspaceId === workspaceId && !this.stopped) return;
    this.disconnect();
    this.workspaceId = workspaceId;
    this.stopped = false;
    this.attempts = 0;
    this.refreshedAfterRefusal = false;
    // A different workspace is a different sequence space entirely.
    this.epoch = null;
    this.open();
  }

  /** Close for good -- sign-out, workspace switch, unmount. */
  disconnect(): void {
    this.stopped = true;
    this.clearTimers();
    const socket = this.socket;
    this.socket = null;
    if (socket) {
      this.detach(socket);
      try {
        socket.close(1000, 'client closing');
      } catch {
        /* already closed */
      }
    }
    this.setOnline([]);
    this.setStatus('idle');
  }

  /**
   * Skip the remaining backoff and try now. Called when the browser reports
   * the network is back or the tab becomes visible: the reason the last
   * attempt failed has probably just gone away.
   */
  reconnectNow(): void {
    if (this.stopped || this.status !== 'reconnecting') return;
    clearTimeout(this.reconnectTimer);
    this.attempts = 0;
    this.open();
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  onStatus(listener: (status: RealtimeStatus) => void): () => void {
    this.statusListeners.add(listener);
    return () => {
      this.statusListeners.delete(listener);
    };
  }

  onPresence(listener: (online: string[]) => void): () => void {
    this.presenceListeners.add(listener);
    return () => {
      this.presenceListeners.delete(listener);
    };
  }

  // ------------------------------------------------------------- socket --
  private open(): void {
    const workspaceId = this.workspaceId;
    if (!workspaceId || this.stopped) return;
    const token = this.deps.getToken();
    if (!token) {
      this.setStatus('unauthorized');
      return;
    }
    this.setStatus(this.attempts > 0 ? 'reconnecting' : 'connecting');

    let socket: WebSocket;
    try {
      socket = this.deps.createSocket(websocketUrl(workspaceId, this.deps.origin()), [
        'bearer',
        token,
      ]);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;
    socket.onmessage = (message: MessageEvent) => {
      if (socket === this.socket) this.handleFrame(socket, message.data);
    };
    socket.onclose = (event: CloseEvent) => {
      if (socket !== this.socket) return;
      this.socket = null;
      this.handleClose(event.code);
    };
    // A failed socket also closes; the close handler decides what happens.
    socket.onerror = () => undefined;
  }

  private detach(socket: WebSocket): void {
    socket.onmessage = null;
    socket.onclose = null;
    socket.onerror = null;
  }

  private handleClose(code: number): void {
    this.stopHeartbeat();
    this.setOnline([]);
    if (this.stopped) return;

    if (code === CLOSE.UNAUTHORIZED) {
      // Usually just an expired access token. One refresh, then give up:
      // looping on a refused credential only produces a stream of 4401s.
      if (this.refreshedAfterRefusal) {
        this.setStatus('unauthorized');
        return;
      }
      this.refreshedAfterRefusal = true;
      void this.deps.refreshToken().then((refreshed) => {
        if (this.stopped) return;
        if (refreshed) this.open();
        else this.setStatus('unauthorized');
      });
      return;
    }

    if (code === CLOSE.FORBIDDEN) {
      // Not something a retry can fix: not a member, a refused origin, or
      // membership withdrawn while connected. Screens need to know.
      this.setStatus('forbidden');
      this.emit(this.synthetic(REVOKED));
      return;
    }

    this.scheduleReconnect(code === CLOSE.TOO_MANY ? TOO_MANY_MIN_DELAY_MS : 0);
  }

  private scheduleReconnect(minimumMs = 0): void {
    this.attempts += 1;
    this.stats.reconnects += 1;
    const ceiling = Math.min(BACKOFF_MAX_MS, BACKOFF_BASE_MS * 2 ** (this.attempts - 1));
    // "Equal jitter": at least half the backoff, so retries still spread
    // out, but never so little that a flapping server is hammered.
    const delay = Math.max(minimumMs, ceiling / 2 + this.deps.random() * (ceiling / 2));
    this.setStatus('reconnecting');
    clearTimeout(this.reconnectTimer);
    this.reconnectTimer = setTimeout(() => this.open(), delay);
  }

  // ------------------------------------------------------------- frames --
  private handleFrame(socket: WebSocket, raw: unknown): void {
    let frame: RealtimeEvent;
    try {
      frame = JSON.parse(String(raw)) as RealtimeEvent;
    } catch {
      return;
    }
    if (!frame || typeof frame.type !== 'string') return;
    // Anything from the server proves the connection is alive.
    this.armDeadline(socket);

    switch (frame.type) {
      case 'connection.ready':
        this.onReady(socket, frame);
        return;
      case 'connection.resumed':
        this.onResumed(frame);
        return;
      case 'connection.pong':
      case 'connection.error':
        return;
      default:
        this.onEvent(frame);
    }
  }

  private onReady(socket: WebSocket, frame: RealtimeEvent): void {
    const data = frame.data as {
      seq: number;
      epoch: string;
      online?: string[];
      heartbeatSeconds?: number;
    };
    this.attempts = 0;
    this.refreshedAfterRefusal = false;
    this.heartbeatMs = Math.max(5, data.heartbeatSeconds ?? DEFAULT_HEARTBEAT_SECONDS) * 1000;
    this.setOnline(data.online ?? []);
    this.startHeartbeat(socket);
    this.setStatus('open');

    if (this.epoch !== null && this.epoch === data.epoch) {
      // Same sequence space: ask for what was missed. From a little behind
      // the highest seen, so a reordered pair split by the disconnect is
      // recovered; repeats are dropped by identity.
      const after = Math.max(this.baseline, this.highest - REORDER_WINDOW);
      this.send(socket, { type: 'resume', after, epoch: data.epoch });
      return;
    }
    const reason = this.epoch === null ? 'initial' : 'epoch-changed';
    this.resetSequence(data.epoch, data.seq);
    this.emit(this.synthetic(RESYNC, { reason }));
  }

  private onResumed(frame: RealtimeEvent): void {
    const data = frame.data as { complete: boolean; seq: number; epoch: string };
    if (data.epoch !== this.epoch) {
      this.resetSequence(data.epoch, data.seq);
      this.emit(this.synthetic(RESYNC, { reason: 'epoch-changed' }));
      return;
    }
    // Everything visible up to data.seq was either replayed or never ours.
    this.highest = Math.max(this.highest, data.seq);
    if (!data.complete) this.emit(this.synthetic(RESYNC, { reason: 'incomplete' }));
  }

  private onEvent(event: RealtimeEvent): void {
    if (event.epoch && event.epoch !== this.epoch) {
      // The counter restarted under us. Adopt the new space, just before
      // this event so it is not itself dropped, and have screens refetch.
      this.resetSequence(event.epoch, event.seq - 1);
      this.emit(this.synthetic(RESYNC, { reason: 'epoch-changed' }));
    }
    if (event.seq <= this.baseline || this.seen.has(event.seq)) {
      this.stats.duplicates += 1;
      return;
    }
    this.remember(event.seq);

    if (event.type === EVENTS.PRESENCE_ONLINE || event.type === EVENTS.PRESENCE_OFFLINE) {
      const online = (event.data as { online?: string[] }).online;
      if (Array.isArray(online)) this.setOnline(online);
    }
    this.emit(event);
  }

  // ---------------------------------------------------------- heartbeat --
  private startHeartbeat(socket: WebSocket): void {
    this.stopHeartbeat();
    this.heartbeatTimer = setInterval(
      () => this.send(socket, { type: 'ping', t: this.deps.now() }),
      this.heartbeatMs
    );
    this.armDeadline(socket);
  }

  /**
   * Give up on a connection that has gone silent. Two missed pongs, plus
   * slack: the server answers every ping, so silence this long means the
   * path is dead even though no close was ever received.
   */
  private armDeadline(socket: WebSocket): void {
    clearTimeout(this.deadlineTimer);
    this.deadlineTimer = setTimeout(() => {
      if (socket !== this.socket) return;
      // Handled here rather than by waiting for onclose: on a dead path the
      // close handshake itself may never complete.
      this.socket = null;
      this.detach(socket);
      try {
        socket.close(CLOSE.HEARTBEAT_LOST, 'heartbeat lost');
      } catch {
        /* already gone */
      }
      this.handleClose(CLOSE.HEARTBEAT_LOST);
    }, this.heartbeatMs * 2 + 5_000);
  }

  private stopHeartbeat(): void {
    clearInterval(this.heartbeatTimer);
    clearTimeout(this.deadlineTimer);
    this.heartbeatTimer = undefined;
    this.deadlineTimer = undefined;
  }

  private clearTimers(): void {
    this.stopHeartbeat();
    clearTimeout(this.reconnectTimer);
    this.reconnectTimer = undefined;
  }

  // ------------------------------------------------------------ helpers --
  private send(socket: WebSocket, payload: Record<string, unknown>): void {
    if (socket.readyState !== OPEN) return;
    try {
      socket.send(JSON.stringify(payload));
    } catch {
      /* the close handler will deal with it */
    }
  }

  private resetSequence(epoch: string, seq: number): void {
    this.epoch = epoch;
    this.baseline = seq;
    this.highest = seq;
    this.seen.clear();
    this.seenOrder = [];
  }

  private remember(seq: number): void {
    this.seen.add(seq);
    this.seenOrder.push(seq);
    if (this.seenOrder.length > SEEN_LIMIT) {
      const evicted = this.seenOrder.shift();
      if (evicted !== undefined) {
        this.seen.delete(evicted);
        // Anything that old is outside any reorder window; keep dropping it.
        this.baseline = Math.max(this.baseline, evicted);
      }
    }
    this.highest = Math.max(this.highest, seq);
  }

  private synthetic(type: string, data: Record<string, unknown> = {}): RealtimeEvent {
    if (type === RESYNC) this.stats.resyncs += 1;
    return {
      type,
      workspace_id: this.workspaceId ?? '',
      data,
      seq: 0,
      epoch: this.epoch ?? '',
      ts: new Date(this.deps.now()).toISOString(),
      actor_id: null,
    };
  }

  private emit(event: RealtimeEvent): void {
    this.listeners.forEach((listener) => {
      try {
        listener(event);
      } catch {
        /* one screen's bug must not stop the others hearing about it */
      }
    });
  }

  private setStatus(status: RealtimeStatus): void {
    if (this.status === status) return;
    this.status = status;
    this.statusListeners.forEach((listener) => listener(status));
  }

  private setOnline(online: string[]): void {
    // Like setStatus: listeners hear about changes, not about every call.
    // Otherwise each connect and disconnect re-renders every presence
    // indicator with the list it already had.
    if (online.length === this.online.length && online.every((id, i) => id === this.online[i])) {
      return;
    }
    this.online = [...online];
    this.presenceListeners.forEach((listener) => listener(this.online));
  }
}
