// Admin → What's New: the Slack channel and switch of the weekday morning post
// of new Cubie release notes, and the switch for the weekly tease. These apply
// to everyone in Cubie.
//
// Spec §4.6.1: RubikSoft writes the release notes once for every workspace, so
// nobody here approves them; each workspace only opts into the digest. The
// tab is an Administration section, like Loose Ends (pages/admin/LooseEndsTab.tsx).
// The Admin page is admin-only (AdminRoute, and Admin.tsx refuses anyone else),
// so this tab needs no refusal of its own; the server answers 403 regardless.
// An old /settings?tab=whats-new link lands here (Settings.tsx).
//
// The server (backend/routers/whats_new_admin.py, under /api/admin/whats-new)
// does the checking: before posting can go on, a channel Slack knows, not
// archived, with Cubie in it. Its refusals are sentences written for this
// card ("Invite Cubie to #announcements first: type /invite @Cubie in that
// channel.") and are shown as they come.
//
// Hook rules (react-hooks/set-state-in-effect is an error in this repo): the
// only effect is the first load, and every setState in it sits inside the
// async callback behind a cancelled flag. The spinner is DERIVED (nothing
// loaded and no error yet), and the form's draft is null until the admin
// touches something, so the saved values show without being copied into state.
import React, { useEffect, useState } from 'react';
import { AlertCircle, CheckCircle2, Loader2, Megaphone, Save } from 'lucide-react';
import { fetchWhatsNewSettings, saveWhatsNewSettings, whatsNewSettingsError } from '../../api/whatsNewAdmin';
import type { WhatsNewSettings } from '../../api/whatsNewAdmin';
import { formatInstant } from '../../utils/datetime';
import {
    channelCheck,
    channelName,
    formFromSettings,
    formProblem,
    isDirty,
    saveButton,
    savedMessage,
    slackStatusLine,
    toInput,
} from './whatsNewSettings';
import type { SettingsForm } from './whatsNewSettings';

interface SwitchRowProps {
    id: string;
    checked: boolean;
    disabled: boolean;
    onChange: (next: boolean) => void;
    label: string;
    description: string;
}

const SwitchRow: React.FC<SwitchRowProps> = ({ id, checked, disabled, onChange, label, description }) => (
    <div className="flex items-start gap-3">
        <button
            type="button"
            role="switch"
            aria-checked={checked}
            aria-labelledby={`${id}-label`}
            aria-describedby={`${id}-description`}
            disabled={disabled}
            onClick={() => onChange(!checked)}
            className={`relative mt-0.5 inline-flex h-6 w-11 flex-shrink-0 items-center rounded-full transition-colors motion-reduce:transition-none disabled:opacity-50 ${
                checked ? 'bg-brand' : 'bg-gray-300'
            }`}
        >
            <span
                className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform motion-reduce:transition-none ${
                    checked ? 'translate-x-6' : 'translate-x-1'
                }`}
            />
        </button>
        <div>
            <p id={`${id}-label`} className="text-sm font-medium text-gray-900">
                {label}
            </p>
            <p id={`${id}-description`} className="mt-1 max-w-2xl text-xs text-gray-500">
                {description}
            </p>
        </div>
    </div>
);

/** The settings themselves (WhatsNewTab adds the heading). */
const WhatsNewSettingsForm: React.FC = () => {
    const [data, setData] = useState<WhatsNewSettings | null>(null);
    const [loadError, setLoadError] = useState<string | null>(null);
    const [draft, setDraft] = useState<SettingsForm | null>(null);
    const [saving, setSaving] = useState(false);
    const [saveError, setSaveError] = useState<string | null>(null);
    const [saved, setSaved] = useState<string | null>(null);

    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const fresh = await fetchWhatsNewSettings();
                if (cancelled) return;
                setData(fresh);
                setLoadError(null);
            } catch (err) {
                if (!cancelled) setLoadError(whatsNewSettingsError(err, "Could not load the What's New settings."));
            }
        })();
        return () => {
            cancelled = true;
        };
    }, []);

    if (data === null && loadError === null) {
        return (
            <div className="flex items-center gap-2 p-6 text-sm text-gray-500">
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> Loading the What&apos;s New settings…
            </div>
        );
    }
    if (data === null) {
        return (
            <div className="flex items-center gap-2 p-6 text-sm text-red-700">
                <AlertCircle className="h-5 w-5" aria-hidden="true" /> {loadError}
            </div>
        );
    }

    const form: SettingsForm = draft ?? formFromSettings(data);
    const dirty = isDirty(form, data);
    const check = channelCheck(data);
    const save = saveButton(dirty, data);
    const typedChannel = channelName(form.channel);

    const clearMessages = () => {
        setSaved(null);
        setSaveError(null);
    };
    const edit = (patch: Partial<SettingsForm>) => {
        clearMessages();
        setDraft({ ...form, ...patch });
    };
    const handleSave = async () => {
        clearMessages();
        const problem = formProblem(form);
        if (problem) {
            setSaveError(problem);
            return;
        }
        setSaving(true);
        try {
            const fresh = await saveWhatsNewSettings(toInput(form));
            setData(fresh);
            setDraft(null);
            setSaved(savedMessage(fresh));
        } catch (err) {
            setSaveError(whatsNewSettingsError(err, 'Could not save. Please try again.'));
        } finally {
            setSaving(false);
        }
    };

    return (
        <div className="space-y-6">
            <section className="rounded-xl border border-gray-100 bg-white p-5" aria-labelledby="wn-settings-slack">
                <h3 id="wn-settings-slack" className="flex items-center gap-2 text-sm font-semibold text-gray-900">
                    <Megaphone className="h-4 w-4 text-brand" aria-hidden="true" />
                    Slack and the drawer
                </h3>
                <p className="mt-1 text-sm text-gray-700">
                    <span className="font-medium">Slack:</span>{' '}
                    <span
                        className={
                            !data.slack_enabled
                                ? 'text-gray-600'
                                : data.bot_in_channel === false || data.channel_archived
                                  ? 'text-amber-700'
                                  : 'text-green-700'
                        }
                    >
                        {slackStatusLine(data)}
                    </span>
                </p>

                <div className="mt-5 max-w-md">
                    <label htmlFor="wn-settings-channel" className="block text-sm font-medium text-gray-700">
                        Slack channel
                    </label>
                    <input
                        id="wn-settings-channel"
                        type="text"
                        value={form.channel}
                        disabled={saving}
                        onChange={e => edit({ channel: e.target.value })}
                        placeholder="#announcements"
                        className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2 text-sm"
                    />
                    <p className="mt-2 flex items-center gap-1.5 text-sm">
                        {check.ok === true ? (
                            <CheckCircle2 className="h-4 w-4 text-green-600" aria-hidden="true" />
                        ) : (
                            <AlertCircle
                                className={`h-4 w-4 ${check.ok === false ? 'text-amber-600' : 'text-gray-400'}`}
                                aria-hidden="true"
                            />
                        )}
                        <span className={check.ok === false ? 'text-amber-700' : 'text-gray-700'}>{check.text}</span>
                    </p>
                    {dirty && typedChannel && typedChannel !== data.channel_name && (
                        <p className="mt-1 text-xs text-gray-500">
                            {form.slack_enabled
                                ? `Save checks ${typedChannel} with Slack.`
                                : `Save keeps ${typedChannel} for when posting is on.`}
                        </p>
                    )}
                </div>

                <div className="mt-5 space-y-5">
                    <SwitchRow
                        id="wn-settings-slack-enabled"
                        checked={form.slack_enabled}
                        disabled={saving}
                        onChange={v => edit({ slack_enabled: v })}
                        label="Post new updates in Slack each weekday morning"
                        description="Each weekday morning Cubie posts the release notes published since the last post in the channel as one message, a line each with a See it link, so people get one notification a day. The first post after turning this on starts from that moment, never from older notes. To turn this on, create the channel in Slack and type /invite @Cubie in it, then Save: Save checks with Slack first. Off, new updates still show in What's new."
                    />
                    <SwitchRow
                        id="wn-settings-tease"
                        checked={form.tease_enabled}
                        disabled={saving}
                        onChange={v => edit({ tease_enabled: v })}
                        label="Tease the drawer the first time someone opens Cubie each week"
                        description="When there is something new since their last visit, the drawer slides open by itself for a few seconds, then closes. Never on a live L10 meeting."
                    />
                </div>
            </section>

            {saveError && (
                <div className="flex items-start gap-2 rounded-md bg-red-50 p-3 text-sm text-red-700" role="alert">
                    <AlertCircle className="mt-0.5 h-4 w-4 flex-shrink-0" aria-hidden="true" /> {saveError}
                </div>
            )}
            {saved && (
                <div className="flex items-center gap-2 rounded-md bg-green-50 p-3 text-sm text-green-700" role="status">
                    <CheckCircle2 className="h-4 w-4" aria-hidden="true" /> {saved}
                </div>
            )}

            <div className="flex flex-wrap items-center gap-3">
                <button
                    type="button"
                    onClick={() => void handleSave()}
                    disabled={saving || !save.enabled}
                    className="inline-flex items-center gap-2 rounded-md bg-brand px-4 py-2 text-sm font-medium text-white hover:bg-brand-hover disabled:opacity-50"
                >
                    {saving ? (
                        <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                    ) : (
                        <Save className="h-4 w-4" aria-hidden="true" />
                    )}
                    {save.label}
                </button>
                {data.updated_by && (
                    <p className="text-xs text-gray-400">
                        Last changed by {data.updated_by}
                        {data.updated_at
                            ? ` on ${formatInstant(data.updated_at, { dateStyle: 'medium', timeStyle: 'short' }, '')}`
                            : ''}
                        . Every save is recorded in the audit log.
                    </p>
                )}
            </div>
        </div>
    );
};

export const WhatsNewTab: React.FC = () => (
    <div className="space-y-4">
        <div>
            <h2 className="text-lg font-semibold text-gray-900">What&apos;s New</h2>
            <p className="mt-1 text-sm text-gray-600">These settings apply to everyone in Cubie.</p>
        </div>
        <WhatsNewSettingsForm />
    </div>
);

export default WhatsNewTab;
