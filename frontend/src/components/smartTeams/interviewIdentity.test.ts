import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { createInterviewIdentityGate, type InterviewTimerScheduler } from './interviewIdentity.ts';
import { mergeOutbox, readOutbox, writeOutbox } from './interviewFlow.ts';

class FakeTimers implements InterviewTimerScheduler {
    private nextId = 1;
    readonly callbacks = new Map<number, () => void>();

    setTimeout(callback: () => void): number {
        const id = this.nextId++;
        this.callbacks.set(id, callback);
        return id;
    }

    clearTimeout(timer: number): void {
        this.callbacks.delete(timer);
    }

    fireAll(): void {
        for (const [id, callback] of this.callbacks) {
            this.callbacks.delete(id);
            callback();
        }
    }
}

describe('interview identity switch', () => {
    it('cancels a pending debounce and retry before another workspace can save it', () => {
        const timers = new FakeTimers();
        let identity = 'workspace-a:user@example.com';
        const old = createInterviewIdentityGate(
            () => identity === 'workspace-a:user@example.com',
            timers,
        );
        const saves: string[] = [];
        const oldEpoch = old.capture();
        old.schedule(oldEpoch, () => saves.push('old pending answer'), 900);
        old.schedule(oldEpoch, () => saves.push('old retry'), 1500);

        identity = 'workspace-b:user@example.com';
        old.invalidate();
        assert.equal(timers.callbacks.size, 0);
        assert.equal(old.isCurrent(oldEpoch), false);

        const current = createInterviewIdentityGate(
            () => identity === 'workspace-b:user@example.com',
            timers,
        );
        current.schedule(current.capture(), () => saves.push('new answer'), 900);
        timers.fireAll();
        assert.deepEqual(saves, ['new answer']);
    });

    it('rejects the previous user immediately, before effect cleanup cancels timers', () => {
        const timers = new FakeTimers();
        let user = 'first@example.com';
        const gate = createInterviewIdentityGate(
            () => user === 'first@example.com',
            timers,
        );
        const epoch = gate.capture();
        let saves = 0;
        gate.schedule(epoch, () => { saves += 1; }, 900);

        user = 'second@example.com';
        assert.equal(gate.isCurrent(epoch), false);
        timers.fireAll();
        assert.equal(saves, 0);
    });

    it('ignores a save that completes after the identity changes and preserves both outboxes', async () => {
        const previous = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
        const values = new Map<string, string>();
        Object.defineProperty(globalThis, 'localStorage', {
            configurable: true,
            value: {
                getItem: (key: string) => values.get(key) ?? null,
                setItem: (key: string, value: string) => { values.set(key, value); },
                removeItem: (key: string) => { values.delete(key); },
            },
        });
        try {
            const oldScope = { workspaceSlug: 'workspace-a', userEmail: 'user@example.com' };
            const newScope = { workspaceSlug: 'workspace-b', userEmail: 'user@example.com' };
            writeOutbox(mergeOutbox(null, 'old-interview', 'big_five', { item_1: 2 }), oldScope);
            writeOutbox(mergeOutbox(null, 'new-interview', 'big_five', { item_1: 5 }), newScope);

            let identity = 'old';
            const gate = createInterviewIdentityGate(() => identity === 'old', new FakeTimers());
            const epoch = gate.capture();
            let finishSave: (() => void) | undefined;
            const response = new Promise<void>(resolve => { finishSave = resolve; });
            let applied = false;
            const save = response.then(() => {
                if (!gate.isCurrent(epoch)) return;
                applied = true;
                writeOutbox(null, oldScope);
            });

            identity = 'new';
            gate.invalidate();
            finishSave?.();
            await save;

            assert.equal(applied, false);
            assert.deepEqual(readOutbox(oldScope)?.answers, { item_1: 2 });
            assert.deepEqual(readOutbox(newScope)?.answers, { item_1: 5 });
        } finally {
            if (previous) Object.defineProperty(globalThis, 'localStorage', previous);
            else Reflect.deleteProperty(globalThis, 'localStorage');
        }
    });

    it('invalidates work from a React effect cleanup even if the same instance activates again', () => {
        const gate = createInterviewIdentityGate(() => true, new FakeTimers());
        const oldEpoch = gate.capture();
        gate.invalidate();
        gate.activate();
        assert.equal(gate.isCurrent(oldEpoch), false);
        assert.equal(gate.isCurrent(gate.capture()), true);
    });
});
