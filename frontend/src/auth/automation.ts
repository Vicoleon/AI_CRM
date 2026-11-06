export type AutomationStatus = 'loading' | 'paused' | 'enabled';

/** Wait for the server profile before starting any automatic work. */
export function automationStatus(
    profile: { automations_enabled?: boolean } | null | undefined,
): AutomationStatus {
    if (!profile) return 'loading';
    // Older servers omit the field; an explicit pause always wins.
    return profile.automations_enabled === false ? 'paused' : 'enabled';
}
