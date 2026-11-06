// The voice command shortcut: what it is for this person on this keyboard, how
// it reads, and how a stream of key events becomes "pressed" / "released".
//
// Default: Option+Command on a Mac, Ctrl+Alt on a PC — modifiers alone, no
// letter. Each person can change it (or turn it off) in Settings → General;
// the choice is saved per kind of keyboard (`mac` / `pc`) in their
// preferences, so it follows them between browsers.
//
// MODIFIERS ALONE ARE THE TRICKY CASE. Option+Command and Ctrl+Alt are also
// the first half of other shortcuts (⌥⌘I opens dev tools; on many European
// keyboards Ctrl+Alt is AltGr, held to type @ or €). So the chord only counts
// when it is pressed and let go with NO other key in between: any other key
// while it is held cancels it ('cancel'), and the page decides what a press
// means only after a short wait (see VoiceCommandBar), which is how typing "@"
// on a German keyboard never starts the microphone.

export type Modifier = 'alt' | 'ctrl' | 'meta' | 'shift';
export type Platform = 'mac' | 'pc';

/** Modifiers alone (`code` null) or modifiers plus one key (`KeyboardEvent.code`). */
export interface Chord {
    mods: Modifier[];
    code: string | null;
}

export type ShortcutChoice = Chord | 'off';

/** As saved in /api/preferences `voice_shortcut`. A platform left out = default. */
export interface VoiceShortcutPref {
    mac?: ShortcutChoice | null;
    pc?: ShortcutChoice | null;
}

export interface KeyLike {
    code: string;
    altKey: boolean;
    ctrlKey: boolean;
    metaKey: boolean;
    shiftKey: boolean;
    repeat?: boolean;
    isComposing?: boolean;
}

export const DEFAULT_CHORDS: Record<Platform, Chord> = {
    mac: { mods: ['alt', 'meta'], code: null },
    pc: { mods: ['ctrl', 'alt'], code: null },
};

export function detectPlatform(): Platform {
    if (typeof navigator === 'undefined') return 'pc';
    const nav = navigator as Navigator & { userAgentData?: { platform?: string } };
    const name = nav.userAgentData?.platform || nav.platform || nav.userAgent || '';
    return /mac|iphone|ipad|ipod/i.test(name) ? 'mac' : 'pc';
}

/** The chord in force for `platform`, or null when the person turned it off. */
export function effectiveChord(pref: VoiceShortcutPref | null | undefined, platform: Platform): Chord | null {
    const choice = pref?.[platform];
    if (choice === 'off') return null;
    return choice && choice.mods?.length ? choice : DEFAULT_CHORDS[platform];
}

// ── Reading a chord ───────────────────────────────────────────────────────────

// Apple's order (⌃⌥⇧⌘) on a Mac; Ctrl, Alt, Shift, Win elsewhere.
const ORDER: Modifier[] = ['ctrl', 'alt', 'shift', 'meta'];
const MAC_SYMBOL: Record<Modifier, string> = { ctrl: '⌃', alt: '⌥', shift: '⇧', meta: '⌘' };
const PC_NAME: Record<Modifier, string> = { ctrl: 'Ctrl', alt: 'Alt', shift: 'Shift', meta: 'Win' };
const MAC_NAME: Record<Modifier, string> = { ctrl: 'Control', alt: 'Option', shift: 'Shift', meta: 'Command' };

const KEY_NAMES: Record<string, string> = {
    Space: 'Space', Enter: 'Enter', Backquote: '`', Minus: '-', Equal: '=', BracketLeft: '[',
    BracketRight: ']', Backslash: '\\', Semicolon: ';', Quote: "'", Comma: ',', Period: '.',
    Slash: '/', ArrowUp: '↑', ArrowDown: '↓', ArrowLeft: '←', ArrowRight: '→',
};

export function keyLabel(code: string): string {
    if (/^Key[A-Z]$/.test(code)) return code.slice(3);
    if (/^Digit\d$/.test(code)) return code.slice(5);
    if (/^Numpad\d$/.test(code)) return `Num ${code.slice(6)}`;
    return KEY_NAMES[code] ?? code;
}

function ordered(mods: Modifier[]): Modifier[] {
    return ORDER.filter(m => mods.includes(m));
}

/** "⌥⌘" on a Mac, "Ctrl+Alt" on a PC (with the key, if there is one). */
export function chordLabel(chord: Chord, platform: Platform): string {
    const mods = ordered(chord.mods);
    if (platform === 'mac') return mods.map(m => MAC_SYMBOL[m]).join('') + (chord.code ? keyLabel(chord.code) : '');
    return [...mods.map(m => PC_NAME[m]), ...(chord.code ? [keyLabel(chord.code)] : [])].join('+');
}

/** Spelled out for a screen reader or a tooltip: "Option+Command". */
export function chordWords(chord: Chord, platform: Platform): string {
    const names = platform === 'mac' ? MAC_NAME : PC_NAME;
    return [...ordered(chord.mods).map(m => names[m]), ...(chord.code ? [keyLabel(chord.code)] : [])].join('+');
}

// ── Key events → press / release ──────────────────────────────────────────────

const MODIFIER_CODES = new Set([
    'ShiftLeft', 'ShiftRight', 'ControlLeft', 'ControlRight', 'AltLeft', 'AltRight',
    'MetaLeft', 'MetaRight', 'OSLeft', 'OSRight', 'AltGraph', 'Fn', 'FnLock', 'CapsLock',
]);

export function isModifierCode(code: string): boolean {
    return MODIFIER_CODES.has(code);
}

export function modsOf(e: KeyLike): Modifier[] {
    const out: Modifier[] = [];
    if (e.ctrlKey) out.push('ctrl');
    if (e.altKey) out.push('alt');
    if (e.shiftKey) out.push('shift');
    if (e.metaKey) out.push('meta');
    return out;
}

function sameMods(a: Modifier[], b: Modifier[]): boolean {
    return a.length === b.length && a.every(m => b.includes(m));
}

export function sameChord(a: Chord | null, b: Chord | null): boolean {
    if (!a || !b) return a === b;
    return a.code === b.code && sameMods(a.mods, b.mods);
}

/** 'arm' — the chord is down; 'cancel' — another key joined it, so it was the
 *  start of something else; 'release' — it was let go, clean. */
export type ChordSignal = 'arm' | 'cancel' | 'release';

/** Turns raw keydown/keyup events into chord signals. One per page. */
export class ChordTracker {
    private armed = false;
    // Another key was pressed during this hold: nothing arms until every
    // modifier is up again.
    private dirty = false;
    private readonly chord: Chord;

    constructor(chord: Chord) {
        this.chord = chord;
    }

    reset(): void {
        this.armed = false;
        this.dirty = false;
    }

    handle(type: 'keydown' | 'keyup', e: KeyLike): ChordSignal | null {
        if (e.isComposing) return null;
        return this.chord.code === null ? this.modifiersOnly(type, e) : this.withKey(type, e);
    }

    private modifiersOnly(type: 'keydown' | 'keyup', e: KeyLike): ChordSignal | null {
        const mods = modsOf(e);
        const modifier = isModifierCode(e.code);
        if (type === 'keydown') {
            if (!modifier) {
                if (mods.length) this.dirty = true;
                if (this.armed) {
                    this.armed = false;
                    return 'cancel';
                }
                return null;
            }
            if (e.repeat) return null;
            if (sameMods(mods, this.chord.mods)) {
                if (!this.dirty && !this.armed) {
                    this.armed = true;
                    return 'arm';
                }
                return null;
            }
            if (this.armed) {
                // A further modifier joined (⌥⌘ became ⌥⇧⌘): a different shortcut.
                this.armed = false;
                this.dirty = true;
                return 'cancel';
            }
            return null;
        }
        // keyup
        if (!mods.length) this.dirty = false;
        if (modifier && this.armed) {
            this.armed = false;
            return 'release';
        }
        return null;
    }

    private withKey(type: 'keydown' | 'keyup', e: KeyLike): ChordSignal | null {
        if (type === 'keydown') {
            if (e.code === this.chord.code && sameMods(modsOf(e), this.chord.mods)) {
                if (e.repeat || this.armed) return null;
                this.armed = true;
                return 'arm';
            }
            if (this.armed && !isModifierCode(e.code)) {
                this.armed = false;
                return 'cancel';
            }
            return null;
        }
        if (e.code === this.chord.code && this.armed) {
            this.armed = false;
            return 'release';
        }
        return null;
    }
}

/** True when the key event is (part of) the chord — the page should not also
 *  act on it (a ⌃Space would otherwise type a space). */
export function isChordKey(chord: Chord, e: KeyLike): boolean {
    return chord.code !== null && e.code === chord.code && sameMods(modsOf(e), chord.mods);
}

// ── Recording a new chord (Settings) ──────────────────────────────────────────

/** Why modifiers + `code` would make a bad shortcut, or null when it is fine. */
export function keyChordProblem(mods: Modifier[], code: string, platform: Platform): string | null {
    const key = keyLabel(code);
    const primary = platform === 'mac' ? ['meta', 'ctrl'] : ['ctrl'];
    if (!mods.some(m => primary.includes(m))) {
        const need = platform === 'mac' ? 'Command or Control' : 'Ctrl';
        return `Hold ${need} with ${key}, so the shortcut never fires while you type.`;
    }
    if (platform === 'pc' && mods.includes('ctrl') && mods.includes('alt')) {
        return `Ctrl+Alt+${key} types a character on many keyboards (AltGr). Pick another key or add Shift instead of Alt.`;
    }
    const plain = /^(Key[A-Z]|Digit\d)$/.test(code);
    if (plain && mods.length === 1) {
        return `${chordLabel({ mods, code }, platform)} is already used by the browser (copy, paste, new tab…). Add Shift, or pick another key.`;
    }
    return null;
}

export type RecordStep = { chord: Chord } | { error: string } | { cancelled: true } | null;

/** Watches the keys pressed in Settings and answers with the chord they make.
 *  Modifiers alone need at least two of them (one alone would fire on every
 *  stray tap of Shift). A key needs Command or Control (Ctrl on a PC) with it,
 *  because the other modifiers TYPE: Shift+A is "A", Option+V is "√" on a Mac,
 *  and Ctrl+Alt+Q is AltGr's "@" on many PC keyboards, and the shortcut would
 *  swallow those characters. Command/Ctrl with a bare letter or digit is
 *  refused too (copy, paste, new tab…). Escape on its own gives up. */
export class ChordRecorder {
    private peak: Modifier[] = [];
    private sawKey = false;

    handle(type: 'keydown' | 'keyup', e: KeyLike, platform: Platform): RecordStep {
        const mods = modsOf(e);
        if (type === 'keydown') {
            if (isModifierCode(e.code)) {
                if (mods.length > this.peak.length) this.peak = mods;
                return null;
            }
            if (e.code === 'Escape' && !mods.length) return { cancelled: true };
            this.sawKey = true;
            const problem = keyChordProblem(mods, e.code, platform);
            if (problem) return { error: problem };
            return { chord: { mods: ordered(mods), code: e.code } };
        }
        if (mods.length || !isModifierCode(e.code)) return null;
        // Everything is up again.
        const peak = this.peak;
        const sawKey = this.sawKey;
        this.peak = [];
        this.sawKey = false;
        if (sawKey || !peak.length) return null;
        if (peak.length < 2) {
            return { error: 'Use at least two keys together, or one with a letter.' };
        }
        return { chord: { mods: ordered(peak), code: null } };
    }
}

// ── Coordination ──────────────────────────────────────────────────────────────

/** Fired (with the new `VoiceShortcutPref | null` as `detail`) when Settings
 *  saves a shortcut, so the live one changes without a reload. */
export const VOICE_SHORTCUT_EVENT = 'cubie:voice-shortcut-changed';

// While Settings is recording a new shortcut, the live one stands down, so
// pressing the old chord to replace it doesn't also start the microphone.
let capturing = 0;

export function suspendShortcut(): () => void {
    capturing += 1;
    let released = false;
    return () => {
        if (released) return;
        released = true;
        capturing = Math.max(0, capturing - 1);
    };
}

export function shortcutSuspended(): boolean {
    return capturing > 0;
}
