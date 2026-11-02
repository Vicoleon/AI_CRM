/* eslint-disable react-refresh/only-export-components -- the imperative
   `confirmDialog` API is intentionally colocated with its single host component. */
import React, { useSyncExternalStore } from 'react';
import { Modal } from './Modal';

// Promise-based replacement for window.confirm(). `await confirmDialog({…})`
// resolves true/false, so a guard clause stays a one-liner:
//   if (!(await confirmDialog({ title, message }))) return;
// A single <ConfirmHost/> (mounted once in Layout) renders the dialog through
// the shared Modal. Escape / backdrop / Cancel all resolve false.

interface ConfirmOptions {
    title: string;
    message: string;
    confirmLabel?: string;
    cancelLabel?: string;
    // Styles the primary button red for deletes / irreversible actions.
    destructive?: boolean;
}
interface PendingConfirm extends ConfirmOptions {
    id: number;
    resolve: (ok: boolean) => void;
}

let pending: PendingConfirm | null = null;
let nextId = 1;
const listeners = new Set<() => void>();
const emit = (): void => {
    for (const l of listeners) l();
};

export const confirmDialog = (opts: ConfirmOptions): Promise<boolean> =>
    new Promise<boolean>(resolve => {
        // If a dialog is somehow already open, resolve it as cancelled first so
        // its awaiter isn't left hanging.
        if (pending) pending.resolve(false);
        pending = { ...opts, id: nextId++, resolve };
        emit();
    });

const settle = (ok: boolean): void => {
    const p = pending;
    if (!p) return;
    pending = null;
    emit();
    p.resolve(ok);
};

const subscribe = (cb: () => void): (() => void) => {
    listeners.add(cb);
    return () => {
        listeners.delete(cb);
    };
};
const getSnapshot = (): PendingConfirm | null => pending;

export const ConfirmHost: React.FC = () => {
    const p = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
    return (
        <Modal
            isOpen={!!p}
            onClose={() => settle(false)}
            title={p?.title ?? ''}
            maxWidth="max-w-md"
        >
            {p && (
                <div>
                    <p className="text-sm text-gray-600 whitespace-pre-line">{p.message}</p>
                    <div className="mt-5 flex justify-end gap-2">
                        <button
                            type="button"
                            onClick={() => settle(false)}
                            className="px-3 py-2 text-sm font-medium text-gray-600 hover:text-gray-800"
                        >
                            {p.cancelLabel ?? 'Cancel'}
                        </button>
                        <button
                            type="button"
                            onClick={() => settle(true)}
                            className={`px-3 py-2 rounded-md text-sm font-medium text-white ${
                                p.destructive
                                    ? 'bg-red-600 hover:bg-red-700'
                                    : 'bg-brand hover:bg-brand-hover'
                            }`}
                        >
                            {p.confirmLabel ?? 'Confirm'}
                        </button>
                    </div>
                </div>
            )}
        </Modal>
    );
};
