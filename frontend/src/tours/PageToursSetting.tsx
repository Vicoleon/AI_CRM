// Settings → General → Page tours: tours on or off, and "Show every tour
// again". Saves on its own, at once, outside the tab's Save button: it is a
// different store, and a switch that needs a second click to stick is a trap.
import React, { useState } from 'react';
import { Loader2, RotateCcw } from 'lucide-react';
import { useTours } from './context';

export const PageToursSetting: React.FC = () => {
    const tours = useTours();
    const [busy, setBusy] = useState<'switch' | 'reset' | null>(null);
    const [note, setNote] = useState<string | null>(null);
    const [error, setError] = useState<string | null>(null);

    const run = async (kind: 'switch' | 'reset', action: () => Promise<void>, done: string) => {
        setBusy(kind);
        setError(null);
        setNote(null);
        try {
            await action();
            setNote(done);
        } catch {
            setError('That didn’t save. Try again in a moment.');
        } finally {
            setBusy(null);
        }
    };

    return (
        <div className="space-y-3">
            <label className="flex items-start gap-2.5 text-sm text-gray-700">
                <input
                    type="checkbox"
                    checked={tours.enabled}
                    disabled={!tours.loaded || busy !== null}
                    onChange={e => {
                        const on = e.target.checked;
                        void run(
                            'switch',
                            () => tours.setEnabled(on),
                            on ? 'Tours will start by themselves again.' : 'Tours won’t start by themselves.',
                        );
                    }}
                    className="mt-0.5 h-4 w-4 rounded border-gray-300 text-brand focus:ring-brand"
                />
                <span>
                    Show a short tour the first time I open a page, and when a page I know gets something new
                </span>
            </label>
            <div className="flex flex-wrap items-center gap-3">
                <button
                    type="button"
                    onClick={() =>
                        void run('reset', tours.resetAll, 'Every tour will show again the next time you open its page.')
                    }
                    disabled={!tours.loaded || busy !== null}
                    className="flex items-center gap-1.5 rounded-md border border-gray-300 px-3 py-1.5 text-xs font-medium text-gray-700 transition-colors hover:bg-gray-50 disabled:opacity-50"
                >
                    {busy === 'reset' ? <Loader2 className="animate-spin" size={12} /> : <RotateCcw size={12} />}
                    Show every tour again
                </button>
                {busy === 'switch' && <Loader2 className="animate-spin text-gray-400" size={14} aria-label="Saving" />}
                <p className="text-xs text-gray-500" role="status">
                    {error ? <span className="text-red-600">{error}</span> : note}
                </p>
            </div>
        </div>
    );
};
