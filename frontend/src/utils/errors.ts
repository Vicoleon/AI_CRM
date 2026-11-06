/**
 * Reading the reason off a failed API call.
 *
 * Every FastAPI error in this app carries a human-readable `detail` string —
 * "You don't have permission to modify this item.", "No current quarter
 * configured", "Owner email not found". A catch block that ignores it and
 * shows its own guess instead turns a precise server answer into a riddle:
 * the EOS Rocks save told everyone to "Verify the owner is a registered user"
 * whatever actually went wrong, which is how a permission refusal was reported
 * as an outage.
 *
 * So: show the server's `detail` when there is one, and keep a fallback only
 * for the cases where there isn't (a network drop, a 502 from the proxy).
 */

/** The `detail` string FastAPI put on an error response, if there is one. */
export const errorDetail = (e: unknown): string | null => {
    const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data
        ?.detail;
    if (typeof detail === 'string' && detail.trim()) return detail.trim();
    // 422s from Pydantic arrive as a list of {loc, msg, type} objects rather
    // than a string — surface the first message so a validation failure still
    // says which field it disliked.
    if (Array.isArray(detail)) {
        const first = detail.find(
            (d): d is { msg: string } =>
                typeof (d as { msg?: unknown })?.msg === 'string' &&
                Boolean((d as { msg: string }).msg.trim()),
        );
        if (first) return first.msg.trim();
    }
    return null;
};

/** The server's `detail`, or `fallback` when the failure carried none. */
export const errorMessage = (e: unknown, fallback: string): string =>
    errorDetail(e) ?? fallback;
