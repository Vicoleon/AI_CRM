import React, { useCallback, useEffect, useRef, useState } from 'react';
import { List, MessageSquarePlus } from 'lucide-react';
import client from '../../api/client';
import { formatInstant } from '../../utils/datetime';
import {
    composerRows,
    continueList,
    noteBlocks,
    toggleBullets,
} from './noteText';

// The threaded-notes surface shared by EOS Issues (discussion →
// resolution) and To-Dos (progress / blockers). Both entities expose the same
// `{basePath}` contract server-side — GET returns the thread oldest-first, POST
// appends — so one component backs both, on the standalone pages and inside the
// L10. Self-contained: lazily fetches on mount and posts new notes.
//
// The composer used to be a two-row box. In UAT that read as "I like to make
// things bullets etc and I am not able to do that without some additional
// room": an IDS note is written live in a meeting, it is usually a list, and
// two rows means typing it through a letterbox. So the box now starts at four
// rows, grows with what you type, can be dragged taller, and understands
// lists — Enter continues a bullet, ⌘/Ctrl+Enter saves, and a stored note is
// re-read as real bullets with a hanging indent instead of a literal "- ".
// The list mechanics live in `noteText.ts`, where they're unit-tested.

interface Note {
    id: string;
    content: string;
    created_by: string | null;
    created_at: string;
}

// No zone label here — every row in a thread is in the reader's own timezone
// and stamping a zone on each one is noise. The session footer names the zone
// once for the meeting; this just has to stop showing UTC.
const _fmt = (iso: string): string =>
    formatInstant(
        iso,
        { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' },
        iso,
    );

/** One stored note, with `- ` lines rendered as bullets. */
const NoteBody: React.FC<{ content: string }> = ({ content }) => (
    <>
        {noteBlocks(content).map((block, i) =>
            block.kind === 'list' ? (
                <ul key={i} className="list-disc pl-4 space-y-0.5 my-0.5 marker:text-gray-400">
                    {block.items.map((item, j) => (
                        <li key={j} className="whitespace-pre-wrap">
                            {item}
                        </li>
                    ))}
                </ul>
            ) : (
                <p key={i} className="whitespace-pre-wrap">
                    {block.text}
                </p>
            ),
        )}
    </>
);

interface NotesThreadProps {
    /** Collection endpoint for this row's notes, e.g. `/api/eos/todos/{id}/notes`. */
    basePath: string;
    /** Shown when the thread is empty — tells the team what belongs here. */
    emptyHint: string;
    /** Placeholder for the compose box. */
    placeholder?: string;
    /** Fired with the thread's length after load and after each add, so a host
     * list can keep its "N notes" indicator honest without refetching counts. */
    onCountChange?: (count: number) => void;
}

export const NotesThread: React.FC<NotesThreadProps> = ({
    basePath,
    emptyHint,
    placeholder = 'Add a note…',
    onCountChange,
}) => {
    const [notes, setNotes] = useState<Note[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [draft, setDraft] = useState('');
    const [submitting, setSubmitting] = useState(false);
    const boxRef = useRef<HTMLTextAreaElement | null>(null);
    // Where to put the caret after a programmatic rewrite (bullet toggle, list
    // continuation). React re-renders with the new value first, so the
    // selection has to be restored in an effect or the caret jumps to the end.
    const pendingSelection = useRef<[number, number] | null>(null);

    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const res = await client.get<Note[]>(basePath);
                if (!cancelled) {
                    setNotes(res.data);
                    onCountChange?.(res.data.length);
                }
            } catch {
                if (!cancelled) setError('Failed to load notes');
            } finally {
                if (!cancelled) setLoading(false);
            }
        })();
        return () => {
            cancelled = true;
        };
        // onCountChange is a reporting callback — refetching when the host
        // re-creates it would double-fetch on every parent render.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [basePath]);

    useEffect(() => {
        const sel = pendingSelection.current;
        if (!sel || !boxRef.current) return;
        pendingSelection.current = null;
        boxRef.current.setSelectionRange(sel[0], sel[1]);
    }, [draft]);

    const applyEdit = useCallback(
        (edit: { value: string; selectionStart: number; selectionEnd: number }) => {
            pendingSelection.current = [edit.selectionStart, edit.selectionEnd];
            setDraft(edit.value);
        },
        [],
    );

    const add = async () => {
        const content = draft.trim();
        if (!content || submitting) return;
        setSubmitting(true);
        try {
            const res = await client.post<Note>(basePath, { content });
            setNotes(prev => {
                const next = [...prev, res.data];
                onCountChange?.(next.length);
                return next;
            });
            setDraft('');
        } catch {
            alert('Failed to add note.');
        } finally {
            setSubmitting(false);
        }
    };

    const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
        // ⌘/Ctrl+Enter saves. Plain Enter has to stay a newline now that a note
        // is expected to be several bullets long.
        if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            void add();
            return;
        }
        if (e.key === 'Enter' && !e.shiftKey) {
            const el = e.currentTarget;
            if (el.selectionStart !== el.selectionEnd) return;
            const edit = continueList(el.value, el.selectionStart);
            if (!edit) return;
            e.preventDefault();
            applyEdit(edit);
        }
    };

    const onBulletClick = () => {
        const el = boxRef.current;
        if (!el) return;
        applyEdit(toggleBullets(el.value, el.selectionStart, el.selectionEnd));
        el.focus();
    };

    return (
        <div className="space-y-2">
            {loading && (
                <p className="text-[11px] text-gray-400 italic">Loading notes…</p>
            )}
            {error && <p className="text-[11px] text-red-500 italic">{error}</p>}
            {!loading && notes.length === 0 && (
                <p className="text-[11px] text-gray-400 italic">{emptyHint}</p>
            )}
            {notes.length > 0 && (
                <ul className="space-y-2">
                    {notes.map(n => (
                        <li
                            key={n.id}
                            className="text-xs text-gray-700 border-l-2 border-gray-200 pl-2"
                        >
                            <NoteBody content={n.content} />
                            <p className="text-[10px] text-gray-400 mt-0.5">
                                {n.created_by ?? 'someone'} · {_fmt(n.created_at)}
                            </p>
                        </li>
                    ))}
                </ul>
            )}
            <div className="pt-1">
                <div className="flex items-start gap-2">
                    <textarea
                        ref={boxRef}
                        value={draft}
                        onChange={e => setDraft(e.target.value)}
                        onKeyDown={onKeyDown}
                        placeholder={placeholder}
                        rows={composerRows(draft)}
                        // `resize-y`: the auto-grow is a floor, not a ceiling —
                        // a long note can still be dragged taller.
                        className="flex-1 min-w-0 resize-y text-xs leading-relaxed rounded border border-gray-300 px-2 py-1.5 focus:border-brand focus:ring-1 focus:ring-brand outline-none"
                    />
                    <div className="flex flex-col gap-1 shrink-0">
                        <button
                            type="button"
                            onClick={add}
                            disabled={submitting || !draft.trim()}
                            className="flex items-center gap-1 px-2.5 py-1.5 text-xs font-medium bg-brand hover:bg-brand-hover disabled:opacity-50 text-white rounded-md"
                        >
                            <MessageSquarePlus size={13} />
                            {submitting ? 'Adding…' : 'Add'}
                        </button>
                        <button
                            type="button"
                            onClick={onBulletClick}
                            className="flex items-center gap-1 px-2.5 py-1.5 text-xs font-medium border border-gray-300 text-gray-600 hover:border-brand hover:text-brand rounded-md"
                            title="Turn the selected lines into bullets"
                        >
                            <List size={13} />
                            Bullets
                        </button>
                    </div>
                </div>
                <p className="mt-1 text-[10px] text-gray-400">
                    Enter starts a new line and continues a bullet · ⌘/Ctrl+Enter saves ·
                    drag the corner for more room
                </p>
            </div>
        </div>
    );
};
