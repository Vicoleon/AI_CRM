// Picking a person by name, against the Workspace directory.
//
// Two callers, one search. Compare uses it to pick who a pair guide is about;
// the support lookup uses it to pick whose profile to debug. The copy
// and the self-exclusion are props because those are the only things that
// differ — a second copy of this component would be a second debounce, a second
// race-token, and a second place to fix the directory fallback below.
//
// WHERE THE NAMES COME FROM, AND WHY IT IS NOT A SMART TEAMS ROUTE
// ---------------------------------------------------------------
// None of the 37 Smart Teams routes lists people. That is deliberate — a Smart
// Teams endpoint that returned a roster would be a directory of who has a
// profile, which is exactly the thing this feature does not publish. So the
// picker searches the Workspace directory instead, on `/api/directory/search`,
// the same endpoint Chat's `@` mention popover uses. It knows nothing about
// Smart Teams: it cannot say whether somebody has taken the interview or
// published a guide, and that is the property that makes it safe to search.
//
// Whether a pair guide can be opened is answered afterwards, by
// `GET /comparison/pair/{other_email}` — 404 for a non-employee, 403 when the
// consent gate is closed.
//
// The directory read needs a non-empty `ADMIN_EMAIL` for domain-wide delegation
// and returns a 500 without it, so the failure is real and the fallback is a
// plain email field rather than a dead end.
import React, { useEffect, useRef, useState } from 'react';
import { AlertTriangle, Loader2, Search, UserRound } from 'lucide-react';

import client from '../../../api/client';
import { PrimaryButton } from '../shared';

/** The shape `/api/directory/search` returns (Google Admin SDK `users.list`
 *  rows, passed through untouched). Every field is optional because the API
 *  omits rather than nulls. */
interface DirectoryUser {
    id?: string;
    primaryEmail?: string;
    name?: { fullName?: string; givenName?: string };
    thumbnailPhotoUrl?: string;
    emails?: { address?: string }[];
}

const emailOf = (user: DirectoryUser): string =>
    (user.primaryEmail || user.emails?.[0]?.address || '').trim();

const nameOf = (user: DirectoryUser): string =>
    (user.name?.fullName || '').trim() || emailOf(user);

const looksLikeEmail = (value: string): boolean =>
    /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value.trim());

export interface ColleaguePickerProps {
    /** The signed-in user's address. Folded out of the results unless
     *  `allowSelf`, because there is no pair guide with yourself and the
     *  Compare route 404s for it. */
    selfEmail: string;
    onPick: (email: string, displayName: string) => void;
    /** Keep the caller in the results. The support lookup sets this:
     *  `can_view_raw_profile` grants subject access, so an admin debugging
     *  their OWN interview is a legitimate — and the safest — thing to do,
     *  and hiding themselves from the picker would make it look forbidden. */
    allowSelf?: boolean;
    /** Copy overrides. Defaulted to the Compare wording so that page is
     *  unchanged; the support lookup asks a different question of the same
     *  directory and must not borrow Compare's promise about pair guides. */
    label?: string;
    hint?: React.ReactNode;
    /** Distinct per mount — two pickers on one page would otherwise share a
     *  DOM id and break the label/`aria-describedby` association. */
    inputId?: string;
}

export const ColleaguePicker: React.FC<ColleaguePickerProps> = ({
    selfEmail,
    onPick,
    allowSelf = false,
    label = 'Who do you want to read about?',
    hint = (
        <>
            A working-relationship guide is written to you about one colleague. It
            needs a published working guide from both of you.
        </>
    ),
    inputId = 'compare-colleague-search',
}) => {
    const [query, setQuery] = useState('');
    const [results, setResults] = useState<DirectoryUser[]>([]);
    const [searching, setSearching] = useState(false);
    const [failed, setFailed] = useState(false);
    const requestRef = useRef(0);

    const needle = query.trim();
    // Derived rather than stored: a query too short to search must clear the
    // panel, and doing that with a setState in the effect body is both a
    // cascading render and a lint error. `results` / `failed` / `searching` are
    // only ever READ through the `tooShort` guard below, so stale values from a
    // longer previous query can never render.
    const tooShort = needle.length < 2;

    useEffect(() => {
        if (tooShort) return;
        const token = ++requestRef.current;
        const timer = setTimeout(() => {
            setSearching(true);
            client
                .get<DirectoryUser[]>('/api/directory/search', {
                    params: { q: needle, limit: 8 },
                })
                .then(({ data }) => {
                    if (requestRef.current !== token) return;
                    setResults(Array.isArray(data) ? data : []);
                    setFailed(false);
                })
                .catch(() => {
                    if (requestRef.current !== token) return;
                    setResults([]);
                    setFailed(true);
                })
                .finally(() => {
                    if (requestRef.current === token) setSearching(false);
                });
        }, 300);
        return () => clearTimeout(timer);
    }, [needle, tooShort]);

    const fold = (value: string) => value.trim().toLowerCase();
    const isSearching = !tooShort && searching;
    const showFailed = !tooShort && failed;
    const visible = tooShort
        ? []
        : results.filter(user => {
              const email = emailOf(user);
              if (!email) return false;
              return allowSelf || fold(email) !== fold(selfEmail);
          });

    const typedEmail = looksLikeEmail(query) ? query.trim() : '';
    const canUseTyped =
        !!typedEmail && (allowSelf || fold(typedEmail) !== fold(selfEmail));

    return (
        <div data-tour="smart-teams-colleague-picker" className="rounded-xl border border-gray-200 bg-white p-5">
            <label htmlFor={inputId} className="block text-sm font-medium text-gray-900">
                {label}
            </label>
            <p className="mt-1 text-xs leading-relaxed text-gray-500">{hint}</p>

            <div className="relative mt-3">
                <Search
                    size={16}
                    aria-hidden="true"
                    className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-gray-400"
                />
                <input
                    id={inputId}
                    type="text"
                    value={query}
                    onChange={event => setQuery(event.target.value)}
                    autoComplete="off"
                    placeholder="Name or email address"
                    aria-describedby={`${inputId}-hint`}
                    className="w-full rounded-md border border-gray-300 py-2 pl-9 pr-3 text-sm text-gray-900 placeholder:text-gray-400 focus:border-brand focus:outline-none focus:ring-1 focus:ring-brand"
                />
            </div>
            <p id={`${inputId}-hint`} className="mt-1.5 text-xs text-gray-500">
                Type at least two characters.
            </p>

            <div className="mt-3" aria-live="polite">
                {isSearching && (
                    <p className="flex items-center gap-2 text-sm text-gray-500">
                        <Loader2 className="animate-spin" size={14} aria-hidden="true" />
                        Searching the directory…
                    </p>
                )}

                {!isSearching && showFailed && (
                    <div className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 p-3">
                        <AlertTriangle
                            size={15}
                            aria-hidden="true"
                            className="mt-0.5 flex-shrink-0 text-amber-600"
                        />
                        <p className="text-xs leading-relaxed text-amber-900">
                            Cubie could not reach the company directory, so it cannot
                            offer names. You can still type a colleague&apos;s full work
                            email address below and continue.
                        </p>
                    </div>
                )}

                {!isSearching && !showFailed && !tooShort && visible.length === 0 && (
                    <p className="text-sm text-gray-500">
                        No one in the directory matches that.
                    </p>
                )}

                {!isSearching && visible.length > 0 && (
                    <ul className="divide-y divide-gray-100 overflow-hidden rounded-lg border border-gray-200">
                        {visible.map(user => {
                            const email = emailOf(user);
                            const name = nameOf(user);
                            return (
                                <li key={user.id || email}>
                                    <button
                                        type="button"
                                        onClick={() => onPick(email, name)}
                                        className="flex w-full items-center gap-3 px-3 py-2.5 text-left transition-colors hover:bg-gray-50 focus:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-inset focus:ring-brand"
                                    >
                                        <span
                                            aria-hidden="true"
                                            className="flex h-8 w-8 flex-shrink-0 items-center justify-center overflow-hidden rounded-full bg-brand-tint text-xs font-semibold text-brand-hover"
                                        >
                                            {user.thumbnailPhotoUrl ? (
                                                <img
                                                    src={user.thumbnailPhotoUrl}
                                                    alt=""
                                                    className="h-full w-full object-cover"
                                                />
                                            ) : (
                                                (name[0] || '?').toUpperCase()
                                            )}
                                        </span>
                                        <span className="min-w-0">
                                            <span className="block truncate text-sm font-medium text-gray-900">
                                                {name}
                                            </span>
                                            <span className="block truncate text-xs text-gray-500">
                                                {email}
                                            </span>
                                        </span>
                                    </button>
                                </li>
                            );
                        })}
                    </ul>
                )}
            </div>

            {canUseTyped && (
                <div className="mt-4 flex flex-wrap items-center gap-3 border-t border-gray-100 pt-4">
                    <UserRound size={16} aria-hidden="true" className="text-gray-400" />
                    <span className="text-sm text-gray-600">
                        Use <span className="font-medium text-gray-900">{typedEmail}</span>
                    </span>
                    <PrimaryButton onClick={() => onPick(typedEmail, typedEmail)}>
                        Continue
                    </PrimaryButton>
                </div>
            )}
        </div>
    );
};

export default ColleaguePicker;
