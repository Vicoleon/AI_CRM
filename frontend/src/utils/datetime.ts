// Reading the API's timestamps as the instants they actually are.
//
// Cubie stores every timestamp as naive UTC (`datetime.utcnow()` into a plain
// `DateTime` column) and serialises it with a bare `.isoformat()`, so the wire
// carries `"2026-09-09T17:01:57.123456"` — a UTC instant wearing no timezone.
// `new Date(...)` on a date-time string with no designator is *specified* to
// read it as LOCAL time, so the browser turns 17:01 UTC into 17:01 wherever
// you happen to be sitting. An L10 that started at 11:01 in Costa Rica
// reported itself as "Session started 5:01:57 PM" — the UTC wall clock, shown
// as if it were yours.
//
// `parseInstant` closes that gap in one place: a timestamp with no timezone
// designator is read as UTC, which is what the server meant. Formatting then
// needs nothing special — `toLocaleString` already renders in the viewer's own
// timezone once the underlying instant is right, so the same session reads
// "11:01 AM" in Costa Rica and "9:01 AM" in Los Angeles.
//
// ── When NOT to use this ───────────────────────────────────────────────────
// An INSTANT is a moment something happened: `actual_start`, `created_at`,
// `solved_at`, `server_now`. Those belong here.
//
// A CALENDAR DATE is not an instant, even when it arrives wearing a midnight
// time — a scorecard week (`"2026-08-30T00:00:00"` meaning *the week of
// Aug 30*) or a to-do's `due_date`. Reading those as UTC shifts them a day
// backwards for anyone west of Greenwich: the week of 8/30 renders as 8/29.
// Those keep the local-midnight reading — see `formatISODate` in
// `pages/eos/_utils.ts`, and leave `_formatWeekHeader` alone.
//
// Deliberately import-free so it loads under Node's test runner without a
// bundler, the same way `pages/eos/_utils.ts` does. For the same reason, plain
// `.ts` modules import it WITH the extension (`'../utils/datetime.ts'`): bare
// Node ESM won't resolve an extensionless specifier the way Vite and tsc do,
// so an extensionless import here breaks that module's own unit tests. `.tsx`
// components can never run under the bare runner anyway, and import it plain.

/** A trailing `Z`, `+HH:MM`, `+HHMM` or `-HH:MM` — the string already says
 *  which timezone it is in, so it is left exactly as it arrived. Google
 *  Calendar (`ev.start`) and anything the client itself produced with
 *  `toISOString()` land here. */
const TZ_DESIGNATOR = /(?:[Zz]|[+-]\d{2}:?\d{2})$/;

/** A bare calendar date, `YYYY-MM-DD`. */
const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;

/**
 * Parse a timestamp from the API into the instant it actually names.
 *
 * - Already carries a timezone (`…Z`, `…+02:00`) → parsed as-is.
 * - No timezone → read as UTC, because that is what the backend stored.
 * - Bare `YYYY-MM-DD` → local midnight, so a calendar date never slides to the
 *   previous day (JS would otherwise read it as UTC midnight).
 *
 * Returns `null` for anything empty or unparseable, so callers render their own
 * placeholder instead of the string "Invalid Date".
 */
export function parseInstant(value: string | null | undefined): Date | null {
    if (!value) return null;
    const raw = String(value).trim();
    if (!raw) return null;

    if (DATE_ONLY.test(raw)) {
        const [y, m, d] = raw.split('-').map(Number);
        const local = new Date(y, m - 1, d);
        return Number.isNaN(local.getTime()) ? null : local;
    }

    // A space between the date and the time is legal ISO 8601 and some
    // serialisers emit it, but the JS Date parse grammar wants a `T`.
    const iso = raw.replace(' ', 'T');
    const parsed = new Date(TZ_DESIGNATOR.test(iso) ? iso : `${iso}Z`);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
}

/**
 * Parse an instant and render it in the viewer's own timezone.
 *
 * `fallback` is what to show when the value is missing or unparseable — an
 * em dash by default; pass the raw string where the old code echoed it back.
 */
export function formatInstant(
    value: string | null | undefined,
    options: Intl.DateTimeFormatOptions = {},
    fallback = '—',
): string {
    const at = parseInstant(value);
    if (!at) return fallback;
    try {
        return at.toLocaleString(undefined, options);
    } catch {
        // A locale/option combination the runtime dislikes must never take a
        // page down over a timestamp.
        return at.toLocaleString();
    }
}

/**
 * Time of day in the viewer's timezone — "11:01 AM", or "11:01 AM CST" with
 * `withZone`.
 *
 * Name the zone wherever the reader might be somewhere else than whoever they
 * are reading about: an L10 is one meeting seen from several timezones at
 * once, and "started 11:01 AM" is only unambiguous if it says whose 11:01 it
 * is. Skip it on a dense list, where a zone on every row is just noise.
 */
export function formatInstantTime(
    value: string | null | undefined,
    { withZone = false, fallback = '—' }: { withZone?: boolean; fallback?: string } = {},
): string {
    return formatInstant(
        value,
        {
            hour: 'numeric',
            minute: '2-digit',
            ...(withZone ? { timeZoneName: 'short' } : {}),
        },
        fallback,
    );
}

/**
 * Calendar date in the viewer's timezone — "9/9/2026".
 *
 * Spelled out rather than `dateStyle: 'short'`, which would render a 2-digit
 * year ("9/9/26"). These are the exact components a bare `toLocaleDateString()`
 * defaults to, so switching a call site to this helper fixes its timezone
 * without also changing how the date looks.
 */
export function formatInstantDate(
    value: string | null | undefined,
    fallback = '—',
): string {
    return formatInstant(
        value,
        { year: 'numeric', month: 'numeric', day: 'numeric' },
        fallback,
    );
}

/** Epoch milliseconds for an instant, or `null` — for elapsed-time maths. */
export function instantMs(value: string | null | undefined): number | null {
    return parseInstant(value)?.getTime() ?? null;
}
