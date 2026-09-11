import React from 'react';
import { act, render } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { useRealtimeEvent } from './useRealtime';
import { RealtimeContext, RealtimeContextValue } from '../contexts/realtimeContextValue';
import type { RealtimeEvent } from '../services/realtime';

function harness() {
  const listeners = new Set<(event: RealtimeEvent) => void>();
  const unsubscribed = vi.fn();
  const subscribe = vi.fn((listener: (event: RealtimeEvent) => void) => {
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
      unsubscribed();
    };
  });
  const value: RealtimeContextValue = { status: 'open', online: [], subscribe };
  const emit = (type: string) =>
    act(() => {
      listeners.forEach((l) =>
        l({ type, workspace_id: 'w', data: {}, seq: 1, epoch: 'E', ts: '', actor_id: null })
      );
    });
  return { value, subscribe, unsubscribed, emit, listeners };
}

const Probe: React.FC<{ types: string[]; onEvent: (event: RealtimeEvent) => void }> = ({
  types,
  onEvent,
}) => {
  useRealtimeEvent(types, onEvent);
  return null;
};

function mount(h: ReturnType<typeof harness>, types: string[], onEvent: (e: RealtimeEvent) => void) {
  const view = render(
    <RealtimeContext.Provider value={h.value}>
      <Probe types={types} onEvent={onEvent} />
    </RealtimeContext.Provider>
  );
  const rerender = (nextTypes: string[], nextHandler: (e: RealtimeEvent) => void) =>
    view.rerender(
      <RealtimeContext.Provider value={h.value}>
        <Probe types={nextTypes} onEvent={nextHandler} />
      </RealtimeContext.Provider>
    );
  return { ...view, rerender };
}

describe('useRealtimeEvent', () => {
  it('subscribes once, however often the handler changes', () => {
    const h = harness();
    const view = mount(h, ['document.created'], () => undefined);
    for (let i = 0; i < 5; i++) view.rerender(['document.created'], () => undefined);
    expect(h.subscribe).toHaveBeenCalledTimes(1);
    expect(h.listeners.size).toBe(1);
  });

  it('always calls the latest handler', () => {
    const h = harness();
    const first = vi.fn();
    const latest = vi.fn();
    const view = mount(h, ['document.created'], first);
    view.rerender(['document.created'], latest);
    h.emit('document.created');
    expect(first).not.toHaveBeenCalled();
    expect(latest).toHaveBeenCalledOnce();
  });

  it('delivers only the requested types', () => {
    const h = harness();
    const handler = vi.fn();
    mount(h, ['document.created'], handler);
    h.emit('member.added');
    h.emit('document.created');
    expect(handler).toHaveBeenCalledOnce();
  });

  it('resubscribes when the types change, without leaving the old listener', () => {
    const h = harness();
    const view = mount(h, ['document.created'], () => undefined);
    view.rerender(['member.added'], () => undefined);
    expect(h.subscribe).toHaveBeenCalledTimes(2);
    expect(h.unsubscribed).toHaveBeenCalledOnce();
    expect(h.listeners.size).toBe(1);
  });

  it('unsubscribes on unmount', () => {
    const h = harness();
    const view = mount(h, ['document.created'], () => undefined);
    view.unmount();
    expect(h.listeners.size).toBe(0);
  });

  it('is harmless with no provider above it', () => {
    expect(() => render(<Probe types={['document.created']} onEvent={() => undefined} />)).not.toThrow();
  });
});
