// Admin → Loose Ends: the daily Slack post's switch and channel.
//
// The post goes to #sales but ships OFF; an admin turns it on and off, and
// changes the channel, here — no env var, no deploy. The server
// (backend/routers/loose_ends_admin.py) does the checking: a channel Slack
// knows, and the Cubie bot in it before the post can go on. Its refusals are
// sentences written for this card and are shown as they come.
//
// Hook rules (react-hooks/set-state-in-effect is an error in this repo): the
// only effect is the first load, and every setState in it sits inside the
// async callback behind a cancelled flag. The spinner is DERIVED (nothing
// loaded and no error yet), and the form's draft is null until the admin
// touches something, so the saved values show without being copied into state.
import React, { useEffect, useState } from 'react';
import { AlertCircle, CheckCircle2, Eye, RefreshCw, Save, Send } from 'lucide-react';
import {
    botCheck,
    digestAdminError,
    digestStatusLine,
    displayChannel,
    fetchDigestSettings,
    lastPostLine,
    previewDigest,
    saveButton,
    saveDigestSettings,
    savedMessage,
} from '../../api/looseEndsAdmin';
import type { LooseEndsDigestPreview, LooseEndsDigestSettings } from '../../api/looseEndsAdmin';
import { formatInstant } from '../../utils/datetime';

interface Draft {
    enabled: boolean;
    channel: string;
}

export const LooseEndsTab: React.FC = () => {
    const [data, setData] = useState<LooseEndsDigestSettings | null>(null);
    const [loadError, setLoadError] = useState<string | null>(null);
    const [draft, setDraft] = useState<Draft | null>(null);
    const [saving, setSaving] = useState(false);
    const [saveError, setSaveError] = useState<string | null>(null);
    const [saved, setSaved] = useState<string | null>(null);
    const [preview, setPreview] = useState<LooseEndsDigestPreview | null>(null);
    const [previewing, setPreviewing] = useState(false);
    const [previewError, setPreviewError] = useState<string | null>(null);

    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const fresh = await fetchDigestSettings();
                if (cancelled) return;
                setData(fresh);
                setLoadError(null);
            } catch (err) {
                if (!cancelled) setLoadError(digestAdminError(err, 'Could not load the Loose Ends post settings.'));
            }
        })();
        return () => {
            cancelled = true;
        };
    }, []);

    if (data === null && loadError === null) {
        return (
            <div className="flex items-center gap-2 p-6 text-sm text-gray-500">
                <RefreshCw className="h-4 w-4 animate-spin" /> Loading the Loose Ends post settings…
            </div>
        );
    }
    if (data === null) {
        return (
            <div className="flex items-center gap-2 p-6 text-sm text-red-700">
                <AlertCircle className="h-5 w-5" /> {loadError}
            </div>
        );
    }

    const form: Draft = draft ?? { enabled: data.enabled, channel: data.channel_name };
    const typedChannel = displayChannel(form.channel);
    const dirty = form.enabled !== data.enabled || typedChannel !== data.channel_name;
    const check = botCheck(data);
    // With nothing changed, Save stays usable while the bot check is not a ✓:
    // every save asks Slack again, so it is the re-check after an invite.
    const save = saveButton(dirty, data);
    const edit = (patch: Partial<Draft>) => {
        setSaved(null);
        setSaveError(null);
        setDraft({ ...form, ...patch });
    };

    const handleSave = async () => {
        setSaving(true);
        setSaveError(null);
        setSaved(null);
        try {
            const fresh = await saveDigestSettings(form.enabled, form.channel);
            setData(fresh);
            setDraft(null);
            // A preview built before the save may contradict the new status.
            setPreview(null);
            setPreviewError(null);
            setSaved(savedMessage(fresh));
        } catch (err) {
            setSaveError(digestAdminError(err, 'Could not save. Please try again.'));
        } finally {
            setSaving(false);
        }
    };

    const handlePreview = async () => {
        setPreviewing(true);
        setPreviewError(null);
        try {
            setPreview(await previewDigest());
        } catch (err) {
            setPreview(null);
            setPreviewError(digestAdminError(err, "Could not build today's post. Please try again."));
        } finally {
            setPreviewing(false);
        }
    };

    return (
        <div className="space-y-6">
            <div className="rounded-xl border border-gray-100 bg-white p-5">
                <h3 className="flex items-center gap-2 text-sm font-semibold text-gray-900">
                    <Send className="h-4 w-4 text-brand" />
                    Loose Ends — daily Slack post
                </h3>
                <p className="mt-1 text-sm text-gray-700">
                    <span className="font-medium">Status:</span>{' '}
                    <span
                        className={
                            !data.enabled
                                ? 'text-gray-600'
                                : data.scan_enabled && !data.channel_archived
                                  ? 'text-green-700'
                                  : 'text-amber-700'
                        }
                    >
                        {digestStatusLine(data)}
                    </span>
                </p>
                <p className="mt-1 text-xs text-gray-500">{lastPostLine(data.last_post)}</p>

                <div className="mt-5 flex items-start gap-3">
                    <button
                        type="button"
                        role="switch"
                        aria-checked={form.enabled}
                        aria-label="Post the daily Loose Ends summary to Slack"
                        disabled={saving}
                        onClick={() => edit({ enabled: !form.enabled })}
                        className={`relative mt-0.5 inline-flex h-6 w-11 flex-shrink-0 items-center rounded-full transition-colors disabled:opacity-50 ${
                            form.enabled ? 'bg-brand' : 'bg-gray-300'
                        }`}
                    >
                        <span
                            className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                                form.enabled ? 'translate-x-6' : 'translate-x-1'
                            }`}
                        />
                    </button>
                    <div>
                        <p className="text-sm font-medium text-gray-900">
                            {form.enabled ? 'Post each weekday morning' : 'Not posting'}
                        </p>
                        <p className="mt-1 max-w-2xl text-xs text-gray-500">
                            After the morning scan, Cubie posts one message listing each person on the Loose
                            Ends roster who has something open: their name, how many loose ends of each kind
                            they have, and how many are 7 days or older, plus anyone Cubie could not check that
                            morning. It never includes an email subject, a message, a client&apos;s words or an
                            AI summary. A quiet day (nothing open and everyone checked) posts nothing. If
                            someone could not be checked, the post goes out to say so.
                        </p>
                    </div>
                </div>

                <div className="mt-5 max-w-md">
                    <label htmlFor="loose-ends-channel" className="block text-sm font-medium text-gray-700">
                        Slack channel
                    </label>
                    <input
                        id="loose-ends-channel"
                        type="text"
                        value={form.channel}
                        disabled={saving}
                        onChange={e => edit({ channel: e.target.value })}
                        placeholder="#sales"
                        className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2 text-sm"
                    />
                    <p className="mt-2 flex items-center gap-1.5 text-sm">
                        {check.ok === true ? (
                            <CheckCircle2 className="h-4 w-4 text-green-600" />
                        ) : (
                            <AlertCircle className={`h-4 w-4 ${check.ok === false ? 'text-amber-600' : 'text-gray-400'}`} />
                        )}
                        <span className={check.ok === false ? 'text-amber-700' : 'text-gray-700'}>{check.text}</span>
                    </p>
                    {dirty && typedChannel && typedChannel !== data.channel_name && (
                        <p className="mt-1 text-xs text-gray-500">Save checks {typedChannel} with Slack.</p>
                    )}
                </div>

                {saveError && (
                    <div className="mt-4 flex items-start gap-2 rounded-md bg-red-50 p-3 text-sm text-red-700">
                        <AlertCircle className="mt-0.5 h-4 w-4 flex-shrink-0" /> {saveError}
                    </div>
                )}
                {saved && (
                    <div className="mt-4 flex items-center gap-2 rounded-md bg-green-50 p-3 text-sm text-green-700">
                        <CheckCircle2 className="h-4 w-4" /> {saved}
                    </div>
                )}

                <div className="mt-5 flex flex-wrap items-center gap-3">
                    <button
                        type="button"
                        onClick={handleSave}
                        disabled={saving || !save.enabled}
                        className="inline-flex items-center gap-2 rounded-md bg-brand px-4 py-2 text-sm font-medium text-white hover:bg-brand-hover disabled:opacity-50"
                    >
                        {saving ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
                        {save.label}
                    </button>
                    <button
                        type="button"
                        onClick={handlePreview}
                        disabled={previewing}
                        className="inline-flex items-center gap-2 rounded-md border border-gray-300 bg-white px-4 py-2 text-sm font-medium text-gray-700 shadow-sm hover:bg-gray-50 disabled:opacity-50"
                    >
                        {previewing ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Eye className="h-4 w-4" />}
                        Preview today&apos;s post
                    </button>
                </div>
                {data.updated_by && (
                    <p className="mt-3 text-xs text-gray-400">
                        Last changed by {data.updated_by}
                        {data.updated_at
                            ? ` on ${formatInstant(data.updated_at, { dateStyle: 'medium', timeStyle: 'short' }, '')}`
                            : ''}
                        .
                    </p>
                )}
            </div>

            {previewError && (
                <div className="flex items-center gap-2 rounded-md bg-red-50 p-3 text-sm text-red-700">
                    <AlertCircle className="h-4 w-4" /> {previewError}
                </div>
            )}
            {preview && (
                <div className="rounded-xl border border-gray-100 bg-white p-5">
                    <h4 className="text-sm font-semibold text-gray-900">Today&apos;s post, as it would read now</h4>
                    <p className={`mt-1 text-sm ${preview.would_post ? 'text-green-700' : 'text-gray-600'}`}>
                        {preview.would_post
                            ? `This would post to ${data.channel_name} after the next morning scan.`
                            : preview.reason}
                    </p>
                    {preview.would_post && preview.reason ? (
                        <p className="mt-1 text-sm text-amber-700">{preview.reason}</p>
                    ) : null}
                    {preview.text ? (
                        <pre className="mt-3 whitespace-pre-wrap break-words rounded-md border border-gray-200 bg-gray-50 p-3 font-sans text-sm text-gray-800">
                            {preview.text}
                        </pre>
                    ) : null}
                    <p className="mt-2 text-xs text-gray-400">Nothing was posted to Slack.</p>
                </div>
            )}
        </div>
    );
};

export default LooseEndsTab;
