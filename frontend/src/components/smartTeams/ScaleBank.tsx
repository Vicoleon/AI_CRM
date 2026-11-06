// The quick-fire scale banks — 155 of the interview's answers come through here.
//
// THE DESIGN CONSTRAINT
// --------------------
// 24 + 24 DISC items, 50 Big Five, 30 motivators, 27 Enneagram. A bank that
// costs one deliberate mouse click per item costs 155 of them, and that is an
// interview people abandon at item 60. So the whole component is built around
// answering from the keyboard without ever reaching for the pointer:
//
//   * Native `<input type="radio">` inside a `<fieldset>` with the statement as
//     its `<legend>`. That is a real radiogroup — arrow keys move and select,
//     screen readers announce "3 of 5", and Tab skips to the NEXT item rather
//     than walking every option. A div-with-role reimplementation would have to
//     re-earn all of that and would get some of it wrong.
//   * Digit keys select directly, with the bounds taken from the section
//     (`value_min`/`value_max`) — never from an assumption that every scale has
//     five points. Forced choice takes A/B or 1/2.
//   * After an answer, focus moves to the next unanswered item on the page, so
//     a page of ten is "4 2 5 3 …" and done. The page does not scroll out from
//     under the person: focus moves with `block: 'nearest'`.
//
// THE STEM IS PART OF THE ITEM
// ----------------------------
// The Big Five bank is administered IPIP-style: every statement starts
// mid-sentence ("Am the life of the party.") and the leading "I…" is carried
// once, by `payload.stem`. A client that ignores that field shows 50 items that
// read as if a word went missing — so the stem is prefixed onto each statement
// here whenever the payload sends one, and nothing is written into this file.
//
// ANCHORS COME FROM THE PAYLOAD
// -----------------------------
// The four Likert layers word their points differently — DISC's "Not like me →
// Very like me" is not Big Five's "Very inaccurate → Very accurate". The legend,
// the per-radio `aria-label` and the mobile inline labels all read
// `payload.scale`, and no anchor string is written in this file.
import React, { useCallback, useEffect, useMemo, useRef } from 'react';
import { Check, ChevronLeft, ChevronRight, Keyboard } from 'lucide-react';

import type {
    AnswerValue,
    ChoicePayload,
    LikertPayload,
    SectionMeta,
} from '../../api/smartTeams';
import {
    PAGE_SIZE,
    answeredCount,
    digitToScaleValue,
    keyToChoice,
    pageCount,
    pageSlice,
    unansweredOnPage,
} from './interviewFlow';
import { PrimaryButton, SecondaryButton } from './shared';

/** DOM id for one radio. Stable so focus can be moved by id rather than by
 *  holding a ref for every input on a page. */
const radioId = (itemId: string, value: string | number) => `st-${itemId}-${value}`;

/** The shared shell for one item: the number, the radiogroup wrapper, and the
 *  keyboard handler. Extracted so the Likert and forced-choice branches share
 *  the chrome without either one having to accept the other's item shape. */
const ItemFieldset: React.FC<{
    number: number;
    legend: string;
    /** Hide the legend text visually — the forced-choice instruction is stated
     *  once above the bank, and repeating it 24 times on screen is noise. It
     *  still labels the radiogroup for assistive technology. */
    legendHidden?: boolean;
    answered: boolean;
    onKeyDown: (event: React.KeyboardEvent<HTMLFieldSetElement>) => void;
    children: React.ReactNode;
}> = ({ number, legend, legendHidden, answered, onKeyDown, children }) => (
    <fieldset
        onKeyDown={onKeyDown}
        aria-describedby="st-scale-legend"
        aria-required
        className={`rounded-lg border bg-white p-4 transition-colors ${
            answered ? 'border-brand-tint bg-brand-tint/30' : 'border-gray-200'
        }`}
    >
        <legend
            className={
                legendHidden
                    ? 'sr-only'
                    : 'mb-3 flex w-full items-start gap-2 px-1 text-sm text-gray-900'
            }
        >
            {!legendHidden && (
                <span className="mt-0.5 flex-shrink-0 font-mono text-xs text-gray-400">
                    {number}.
                </span>
            )}
            <span className={legendHidden ? '' : 'font-medium leading-relaxed'}>{legend}</span>
        </legend>
        {legendHidden && (
            <p aria-hidden className="mb-3 px-1 font-mono text-xs text-gray-400">
                {number}.
            </p>
        )}
        {children}
    </fieldset>
);

interface ScaleBankProps {
    section: SectionMeta;
    payload: LikertPayload | ChoicePayload;
    answers: Record<string, AnswerValue>;
    onAnswer: (itemId: string, value: AnswerValue) => void;
    page: number;
    onPageChange: (page: number) => void;
    /** Called when every item in the bank is answered and they press Continue. */
    onComplete: () => void;
    completing: boolean;
}

export const ScaleBank: React.FC<ScaleBankProps> = ({
    section,
    payload,
    answers,
    onAnswer,
    page,
    onPageChange,
    onComplete,
    completing,
}) => {
    const itemIds = useMemo(
        () => (payload.items as ReadonlyArray<{ item_id: string }>).map(item => item.item_id),
        [payload.items],
    );
    const totalPages = pageCount(itemIds.length);
    const visibleIds = useMemo(() => pageSlice(itemIds, page), [itemIds, page]);
    const firstOffset = (page - 1) * PAGE_SIZE;

    const answered = answeredCount(itemIds, answers);
    const missingOnPage = unansweredOnPage(visibleIds, answers);
    const missingInBank = unansweredOnPage(itemIds, answers);
    const isLastPage = page >= totalPages;

    // Scroll the page's first item into view on a page change, so "Next" does
    // not leave the reader looking at the footer of the page they just left.
    const topRef = useRef<HTMLDivElement>(null);
    useEffect(() => {
        topRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }, [page]);

    const firstRadioValue = payload.answer_kind === 'likert' ? payload.scale[0]?.value ?? 1 : 'a';

    /** Move to the next item that still needs an answer, so answering a page is
     *  one continuous keyboard run. Deliberately the next UNANSWERED item rather
     *  than the next item: someone revising item 3 of a finished page should not
     *  be dragged to item 4. */
    const focusNext = useCallback(
        (fromItemId: string) => {
            const from = visibleIds.indexOf(fromItemId);
            const target = visibleIds
                .slice(from + 1)
                .find(id => answers[id] === undefined || answers[id] === null);
            if (!target) return;
            const el = document.getElementById(radioId(target, firstRadioValue));
            el?.focus({ preventScroll: true });
            el?.scrollIntoView({ block: 'nearest' });
        },
        [answers, firstRadioValue, visibleIds],
    );

    const select = useCallback(
        (itemId: string, value: AnswerValue) => {
            onAnswer(itemId, value);
            focusNext(itemId);
        },
        [focusNext, onAnswer],
    );

    /** Digit / letter accelerators. Native radios handle the arrow keys
     *  themselves, so there is nothing to fight here — this only adds the keys
     *  they ignore. */
    const onItemKeyDown = useCallback(
        (event: React.KeyboardEvent<HTMLFieldSetElement>, itemId: string) => {
            if (event.metaKey || event.ctrlKey || event.altKey) return;
            if (payload.answer_kind === 'likert') {
                const min = section.value_min ?? payload.scale[0]?.value ?? 1;
                const max =
                    section.value_max ?? payload.scale[payload.scale.length - 1]?.value ?? 5;
                const value = digitToScaleValue(event.key, min, max);
                if (value === null) return;
                event.preventDefault();
                select(itemId, value);
                return;
            }
            const choice = keyToChoice(event.key);
            if (!choice) return;
            event.preventDefault();
            select(itemId, choice);
        },
        [payload, section.value_max, section.value_min, select],
    );

    return (
        <div ref={topRef}>
            {/* The scale, pinned above the items so it stays readable while
                answering. Named by id and pointed at from every fieldset with
                aria-describedby, so an assistive reader gets the anchors with
                each item without them being read out five times over. */}
            <div
                data-tour="smart-teams-interview-scale"
                id="st-scale-legend"
                className="sticky top-0 z-10 mb-4 rounded-lg border border-gray-200 bg-white/95 p-3 backdrop-blur"
            >
                {payload.answer_kind === 'likert' ? (
                    <>
                        <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5">
                            {payload.scale.map(point => (
                                <span
                                    key={point.value}
                                    className="inline-flex items-center gap-1.5 text-xs text-gray-600"
                                >
                                    <span className="inline-flex h-5 w-5 items-center justify-center rounded border border-gray-300 bg-gray-50 font-mono text-[11px] text-gray-700">
                                        {point.value}
                                    </span>
                                    {point.label}
                                </span>
                            ))}
                        </div>
                        <p className="mt-2 flex items-center gap-1.5 text-[11px] text-gray-500">
                            <Keyboard size={12} />
                            Press {payload.scale[0]?.value}–
                            {payload.scale[payload.scale.length - 1]?.value} to answer, or use
                            the arrow keys. Answering jumps to the next statement.
                        </p>
                    </>
                ) : (
                    <>
                        <p className="text-xs text-gray-600">{payload.instruction}</p>
                        <p className="mt-2 flex items-center gap-1.5 text-[11px] text-gray-500">
                            <Keyboard size={12} />
                            Press A or B (1 or 2 also work), or use the arrow keys.
                        </p>
                    </>
                )}
            </div>

            {/* Live count, so a keyboard-only run gets the same feedback the bar
                gives a sighted reader. Polite: it must not interrupt the anchor
                being read out. */}
            <p className="sr-only" aria-live="polite">
                {answered} of {itemIds.length} answered in this section.
            </p>

            <ol className="space-y-3">
                {payload.answer_kind === 'likert'
                    ? pageSlice(payload.items, page).map((item, index) => {
                          const current = answers[item.item_id];
                          const isAnswered = current !== undefined && current !== null;
                          return (
                              <li key={item.item_id}>
                                  <ItemFieldset
                                      number={firstOffset + index + 1}
                                      legend={
                                          payload.stem
                                              ? `${payload.stem} ${item.prompt}`
                                              : item.prompt
                                      }
                                      answered={isAnswered}
                                      onKeyDown={event => onItemKeyDown(event, item.item_id)}
                                  >
                                      <div className="flex flex-col gap-2 sm:flex-row sm:items-stretch">
                                          {payload.scale.map(point => {
                                              const checked = current === point.value;
                                              return (
                                                  <label
                                                      key={point.value}
                                                      htmlFor={radioId(item.item_id, point.value)}
                                                      className={`flex flex-1 cursor-pointer items-center justify-center gap-2 rounded-md border px-3 py-2 text-sm transition-colors focus-within:ring-2 focus-within:ring-brand focus-within:ring-offset-1 ${
                                                          checked
                                                              ? 'border-brand bg-brand text-white'
                                                              : 'border-gray-300 bg-white text-gray-700 hover:border-brand hover:bg-brand-tint'
                                                      }`}
                                                  >
                                                      <input
                                                          id={radioId(item.item_id, point.value)}
                                                          type="radio"
                                                          name={item.item_id}
                                                          value={point.value}
                                                          checked={checked}
                                                          onChange={() =>
                                                              select(item.item_id, point.value)
                                                          }
                                                          // The visible label is the control;
                                                          // the input stays in the a11y tree
                                                          // and keeps focus.
                                                          className="sr-only"
                                                          aria-label={`${point.value} — ${point.label}`}
                                                      />
                                                      <span className="font-mono text-xs opacity-70">
                                                          {point.value}
                                                      </span>
                                                      {/* Anchors repeat inline on narrow
                                                          screens, where the sticky legend
                                                          is a scroll away. */}
                                                      <span className="text-xs sm:hidden">
                                                          {point.label}
                                                      </span>
                                                  </label>
                                              );
                                          })}
                                      </div>
                                  </ItemFieldset>
                              </li>
                          );
                      })
                    : pageSlice(payload.items, page).map((item, index) => {
                          const current = answers[item.item_id];
                          const isAnswered = current !== undefined && current !== null;
                          const options = [
                              { value: 'a' as const, text: item.option_a, key: 'A' },
                              { value: 'b' as const, text: item.option_b, key: 'B' },
                          ];
                          return (
                              <li key={item.item_id}>
                                  <ItemFieldset
                                      number={firstOffset + index + 1}
                                      legend={payload.instruction}
                                      legendHidden
                                      answered={isAnswered}
                                      onKeyDown={event => onItemKeyDown(event, item.item_id)}
                                  >
                                      <div className="flex flex-col gap-2 sm:flex-row">
                                          {options.map(option => {
                                              const checked = current === option.value;
                                              return (
                                                  <label
                                                      key={option.value}
                                                      htmlFor={radioId(item.item_id, option.value)}
                                                      className={`flex flex-1 cursor-pointer items-start gap-2.5 rounded-md border px-3 py-2.5 text-sm transition-colors focus-within:ring-2 focus-within:ring-brand focus-within:ring-offset-1 ${
                                                          checked
                                                              ? 'border-brand bg-brand text-white'
                                                              : 'border-gray-300 bg-white text-gray-700 hover:border-brand hover:bg-brand-tint'
                                                      }`}
                                                  >
                                                      <input
                                                          id={radioId(
                                                              item.item_id,
                                                              option.value,
                                                          )}
                                                          type="radio"
                                                          name={item.item_id}
                                                          value={option.value}
                                                          checked={checked}
                                                          onChange={() =>
                                                              select(item.item_id, option.value)
                                                          }
                                                          className="sr-only"
                                                          aria-label={`${option.key}: ${option.text}`}
                                                      />
                                                      <span
                                                          className={`mt-0.5 flex h-5 w-5 flex-shrink-0 items-center justify-center rounded border font-mono text-[11px] ${
                                                              checked
                                                                  ? 'border-white/60 text-white'
                                                                  : 'border-gray-300 text-gray-500'
                                                          }`}
                                                      >
                                                          {option.key}
                                                      </span>
                                                      <span className="leading-relaxed">
                                                          {option.text}
                                                      </span>
                                                  </label>
                                              );
                                          })}
                                      </div>
                                  </ItemFieldset>
                              </li>
                          );
                      })}
            </ol>

            {/* Footer: page position, what is still missing, and the way on. The
                Continue guard names the count rather than just greying out — a
                disabled button with no stated reason is a dead end. */}
            <div className="mt-5 flex flex-col gap-3 border-t border-gray-200 pt-4 sm:flex-row sm:items-center sm:justify-between">
                <p className="text-xs text-gray-500">
                    Page {page} of {totalPages}
                    {missingOnPage.length > 0 && (
                        <>
                            {' · '}
                            <span className="text-amber-700">
                                {missingOnPage.length} still to answer on this page
                            </span>
                        </>
                    )}
                </p>
                <div className="flex items-center gap-2">
                    {page > 1 && (
                        <SecondaryButton onClick={() => onPageChange(page - 1)}>
                            <ChevronLeft size={15} />
                            Back
                        </SecondaryButton>
                    )}
                    {!isLastPage ? (
                        <PrimaryButton onClick={() => onPageChange(page + 1)}>
                            Next
                            <ChevronRight size={15} />
                        </PrimaryButton>
                    ) : (
                        <PrimaryButton
                            onClick={onComplete}
                            busy={completing}
                            disabled={missingInBank.length > 0}
                        >
                            <Check size={15} />
                            Finish this section
                        </PrimaryButton>
                    )}
                </div>
            </div>
            {isLastPage && missingInBank.length > 0 && (
                <p className="mt-2 text-right text-xs text-amber-700">
                    {missingInBank.length === 1
                        ? '1 statement earlier in this section still needs an answer.'
                        : `${missingInBank.length} statements in this section still need an answer.`}
                </p>
            )}
        </div>
    );
};
