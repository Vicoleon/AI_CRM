/* eslint-disable react-refresh/only-export-components -- the imperative `toast`
   API is intentionally colocated with its single host component. */
import React, { useSyncExternalStore, useCallback } from 'react';
import { createPortal } from 'react-dom';
import { CheckCircle2, AlertCircle, Info, X } from 'lucide-react';

// Lightweight, dependency-free toast. Imperative API (`toast.error('…')`) backed
// by a module-level store so any call site can fire one without prop-drilling a
// hook or context. A single <ToastHost/> (mounted once in Layout) renders the
// stack via a portal — mounting it more than once would double every toast.

export type ToastKind = 'success' | 'error' | 'info';
interface ToastItem {
    id: number;
    kind: ToastKind;
    message: string;
}

let items: ToastItem[] = [];
let nextId = 1;
const listeners = new Set<() => void>();
const emit = (): void => {
    for (const l of listeners) l();
};

const remove = (id: number): void => {
    items = items.filter(t => t.id !== id);
    emit();
};

const push = (kind: ToastKind, message: string): number => {
    const id = nextId++;
    items = [...items, { id, kind, message }];
    emit();
    // Errors linger a little longer than confirmations.
    window.setTimeout(() => remove(id), kind === 'error' ? 6000 : 4000);
    return id;
};

export const toast = {
    success: (message: string): number => push('success', message),
    error: (message: string): number => push('error', message),
    info: (message: string): number => push('info', message),
};

const subscribe = (cb: () => void): (() => void) => {
    listeners.add(cb);
    return () => {
        listeners.delete(cb);
    };
};
const getSnapshot = (): ToastItem[] => items;

const TONE: Record<
    ToastKind,
    { border: string; bg: string; text: string; icon: React.ComponentType<{ size?: number; className?: string }>; iconCls: string }
> = {
    success: { border: 'border-green-200', bg: 'bg-green-50', text: 'text-green-800', icon: CheckCircle2, iconCls: 'text-green-600' },
    error: { border: 'border-red-200', bg: 'bg-red-50', text: 'text-red-800', icon: AlertCircle, iconCls: 'text-red-600' },
    info: { border: 'border-gray-200', bg: 'bg-white', text: 'text-gray-800', icon: Info, iconCls: 'text-brand' },
};

export const ToastHost: React.FC = () => {
    const toasts = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
    const onClose = useCallback((id: number) => remove(id), []);
    if (typeof document === 'undefined') return null;
    return createPortal(
        <div
            className="fixed bottom-4 right-4 z-[100] flex flex-col gap-2 w-80 max-w-[calc(100vw-2rem)]"
            aria-live="polite"
        >
            {toasts.map(t => {
                const tone = TONE[t.kind];
                const Icon = tone.icon;
                return (
                    <div
                        key={t.id}
                        role="status"
                        className={`flex items-start gap-2.5 rounded-lg border ${tone.border} ${tone.bg} ${tone.text} shadow-md px-3.5 py-2.5 text-sm`}
                    >
                        <Icon size={16} className={`${tone.iconCls} mt-0.5 shrink-0`} />
                        <span className="flex-1 min-w-0 break-words">{t.message}</span>
                        <button
                            type="button"
                            onClick={() => onClose(t.id)}
                            className="text-gray-400 hover:text-gray-700 shrink-0"
                            aria-label="Dismiss notification"
                        >
                            <X size={14} />
                        </button>
                    </div>
                );
            })}
        </div>,
        document.body,
    );
};
