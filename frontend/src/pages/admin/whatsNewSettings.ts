// Admin → What's New: the form's rules and sentences, kept pure so they run
// under Node's test runner (`whatsNewSettings.test.ts`).
//
// * The Slack channel of the weekday morning post of new Cubie release notes,
//   and whether Cubie can post in it, said with the fix ("type /invite @Cubie
//   there, then Save").
// * Save stays usable with nothing changed while posting is on and the channel
//   check is not a ✓: every save asks Slack again, so it is the re-check.
//
// Spec §4.6.1: the notes come from RubikSoft, so there is nobody to approve
// them here; the approvers left with the approval flow.
//
// The server (`rubik_core/whats_new/settings.py`) checks everything again and
// its 400 sentence is shown as it comes; these checks only say it sooner.
// Bare Node loads it: the product name comes from the import-free
// `brand/names.ts`, and the API types are a type-only import.
import { PRODUCT_NAME } from '../../brand/names.ts';
import type { WhatsNewSettings, WhatsNewSettingsInput } from '../../api/whatsNewAdmin';

export interface SettingsForm {
    slack_enabled: boolean;
    channel: string;
    tease_enabled: boolean;
}

export const formFromSettings = (s: WhatsNewSettings): SettingsForm => ({
    slack_enabled: s.slack_enabled,
    channel: s.channel_name,
    tease_enabled: s.tease_enabled,
});

/** `#announcements` from whatever was typed (`announcements`,
 *  ` #Announcements `); a channel id (bare, upper-case, with a digit) stays as
 *  it is. Empty in, empty out. */
export function channelName(typed: string): string {
    const trimmed = typed.trim();
    const bare = trimmed.replace(/^#+/, '').trim();
    if (!bare) return '';
    const isId = !trimmed.startsWith('#') && /^[CG][A-Z0-9]{6,}$/.test(bare) && /\d/.test(bare);
    return isId ? bare : `#${bare.toLowerCase()}`;
}

/** The PUT body. */
export const toInput = (form: SettingsForm): WhatsNewSettingsInput => ({
    slack_enabled: form.slack_enabled,
    channel: form.channel.trim(),
    tease_enabled: form.tease_enabled,
});

/** Whether the form differs from what is saved. */
export function isDirty(form: SettingsForm, saved: WhatsNewSettings): boolean {
    return (
        form.slack_enabled !== saved.slack_enabled ||
        channelName(form.channel) !== saved.channel_name ||
        form.tease_enabled !== saved.tease_enabled
    );
}

/** What the server would refuse before Slack is even asked, or null. */
export function formProblem(form: SettingsForm): string | null {
    if (form.slack_enabled && !channelName(form.channel)) return 'Type the Slack channel to post in.';
    return null;
}

type SlackFields = Pick<WhatsNewSettings, 'slack_enabled' | 'channel_name' | 'bot_in_channel' | 'channel_archived'>;

/** The one-line Slack status at the top of the card. */
export function slackStatusLine(s: SlackFields): string {
    if (!s.slack_enabled) return "Off — new updates show in What's new; nothing is posted in Slack";
    if (s.channel_archived) return `On for ${s.channel_name}, but Slack says the channel is archived, so nothing posts`;
    if (s.bot_in_channel === false) {
        return `On for ${s.channel_name}, but ${PRODUCT_NAME} isn't in the channel, so nothing posts`;
    }
    return `On — new updates go out in ${s.channel_name} in one post each weekday morning`;
}

export interface ChannelCheck {
    /** true = ✓, false = needs fixing, null = not checked yet. */
    ok: boolean | null;
    text: string;
}

/** Whether Cubie can post in the channel, said plainly with the fix. */
export function channelCheck(s: Pick<WhatsNewSettings, 'channel_name' | 'bot_in_channel' | 'channel_archived'>): ChannelCheck {
    const name = s.channel_name;
    if (s.channel_archived) return { ok: false, text: `Slack says ${name} is archived — pick another channel and Save` };
    if (s.bot_in_channel === true) return { ok: true, text: `${PRODUCT_NAME} is in ${name}` };
    if (s.bot_in_channel === false) {
        return {
            ok: false,
            text: `${PRODUCT_NAME} isn't in ${name} yet — type /invite @${PRODUCT_NAME} in ${name}, then Save to re-check`,
        };
    }
    return { ok: null, text: `Create ${name} in Slack and type /invite @${PRODUCT_NAME} there, then Save` };
}

/** The Save button: enabled when something changed, and with nothing changed
 *  while posting is on and the channel check is not a ✓ (the re-check). */
export function saveButton(dirty: boolean, s: SlackFields): { enabled: boolean; label: string } {
    if (dirty) return { enabled: true, label: 'Save' };
    const recheck = s.slack_enabled && (s.bot_in_channel !== true || s.channel_archived);
    return recheck ? { enabled: true, label: 'Save and re-check' } : { enabled: false, label: 'Save' };
}

/** The green line after a save. */
export function savedMessage(s: SlackFields): string {
    if (!s.slack_enabled) return "Saved. New updates show in What's new; nothing is posted in Slack until posting is on.";
    return `Saved. New updates go out in ${s.channel_name} in one post each weekday morning.`;
}
