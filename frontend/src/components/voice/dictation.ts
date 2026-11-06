// Put dictated words into the text box they were spoken for.
//
// The words always go at the END. Pressing the mic takes focus off the box, so
// there is no caret to honour; the end is the one predictable place, and the
// caret is left there. Whether they are then sent is the caller's decision
// (Ask Cubie sends at once; a form would leave them for the person).
import { joinDictation } from './voiceActivity';

/** Append to a contentEditable box (Ask Cubie). Returns false if nothing was added. */
export function appendToEditable(el: HTMLElement, text: string): boolean {
    // A cleared contentEditable can keep a trailing <br>; text after it would
    // start on a second line.
    if (el.lastChild?.nodeName === 'BR') el.removeChild(el.lastChild);
    const addition = joinDictation(el.textContent ?? '', text);
    if (!addition) return false;
    el.appendChild(document.createTextNode(addition));
    el.focus();
    const range = document.createRange();
    range.selectNodeContents(el);
    range.collapse(false);
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);
    return true;
}

/** Append to a textarea or input, firing `input` so React state follows. */
export function appendToField(el: HTMLTextAreaElement | HTMLInputElement, text: string): boolean {
    const addition = joinDictation(el.value, text);
    if (!addition) return false;
    // React tracks the value through the native setter; assigning `el.value`
    // directly would be overwritten on the next render.
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value')?.set?.call(el, el.value + addition);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.focus();
    el.setSelectionRange(el.value.length, el.value.length);
    return true;
}
