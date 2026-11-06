// Settings → General: the voice command shortcut. Shows the one in force on
// this keyboard, records a new one from the keys pressed, puts the default
// back, or turns it off. The choice is per kind of keyboard (a Mac and a PC
// keep their own), and is saved with the rest of the General tab.
import React, { useEffect, useState } from 'react';

import {
    ChordRecorder,
    DEFAULT_CHORDS,
    chordLabel,
    chordWords,
    detectPlatform,
    effectiveChord,
    sameChord,
    suspendShortcut,
} from './shortcut';
import type { VoiceShortcutPref } from './shortcut';

const BUTTON = 'rounded-md border border-gray-300 px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40';

export const VoiceShortcutSetting: React.FC<{
    value: VoiceShortcutPref | null;
    onChange: (next: VoiceShortcutPref | null) => void;
}> = ({ value, onChange }) => {
    const [platform] = useState(detectPlatform);
    const [recording, setRecording] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const chord = effectiveChord(value, platform);
    const isDefault = sameChord(chord, DEFAULT_CHORDS[platform]);
    const other = platform === 'mac' ? 'pc' : 'mac';
    const otherChord = effectiveChord(value, other);

    const set = (choice: VoiceShortcutPref[typeof platform]) => {
        const next: VoiceShortcutPref = { ...(value ?? {}) };
        if (choice) next[platform] = choice;
        else delete next[platform];
        onChange(Object.keys(next).length ? next : null);
    };

    // While recording, every key goes here, and the live shortcut stands down.
    useEffect(() => {
        if (!recording) return;
        const resume = suspendShortcut();
        const recorder = new ChordRecorder();
        const onKey = (e: KeyboardEvent) => {
            e.preventDefault();
            e.stopPropagation();
            const step = recorder.handle(e.type === 'keydown' ? 'keydown' : 'keyup', e, platform);
            if (!step) return;
            if ('cancelled' in step) setRecording(false);
            else if ('error' in step) setError(step.error);
            else {
                setError(null);
                setRecording(false);
                set(sameChord(step.chord, DEFAULT_CHORDS[platform]) ? null : step.chord);
            }
        };
        window.addEventListener('keydown', onKey, true);
        window.addEventListener('keyup', onKey, true);
        return () => {
            window.removeEventListener('keydown', onKey, true);
            window.removeEventListener('keyup', onKey, true);
            resume();
        };
    }, [recording]); // eslint-disable-line react-hooks/exhaustive-deps

    return (
        <div>
            <div className="flex flex-wrap items-center gap-2">
                {recording ? (
                    <span
                        className="rounded-md border-2 border-dashed border-brand/50 bg-brand/5 px-3 py-1.5 text-sm text-brand"
                        role="status"
                        aria-live="polite"
                    >
                        Press the keys you want… (Esc to cancel)
                    </span>
                ) : chord ? (
                    <kbd
                        className="rounded-md border border-gray-300 bg-gray-50 px-2.5 py-1 font-sans text-sm font-semibold text-gray-900"
                        title={chordWords(chord, platform)}
                    >
                        {chordLabel(chord, platform)}
                    </kbd>
                ) : (
                    <span className="text-sm text-gray-500">Off — use the Voice button instead.</span>
                )}
                {!recording && (
                    <button type="button" className={BUTTON} onClick={() => { setError(null); setRecording(true); }}>
                        Change
                    </button>
                )}
                {recording && (
                    <button type="button" className={BUTTON} onClick={() => setRecording(false)}>
                        Cancel
                    </button>
                )}
                {!recording && (!isDefault || !chord) && (
                    <button type="button" className={BUTTON} onClick={() => set(null)}>
                        Use {chordLabel(DEFAULT_CHORDS[platform], platform)}
                    </button>
                )}
                {!recording && chord && (
                    <button type="button" className={BUTTON} onClick={() => set('off')}>
                        Turn off
                    </button>
                )}
            </div>
            {error && <p className="mt-2 text-xs text-red-600">{error}</p>}
            <p className="mt-2 text-xs text-gray-500">
                This is for {platform === 'mac' ? 'a Mac' : 'a PC'} keyboard. On {other === 'mac' ? 'a Mac' : 'a PC'} it
                is {otherChord ? chordLabel(otherChord, other) : 'off'}; change it from there.
            </p>
        </div>
    );
};

export default VoiceShortcutSetting;
