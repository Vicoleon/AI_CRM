// The Daily Briefing's spotlight line, kept pure so it runs under Node's test
// runner (`spotlight.test.ts`): the newest unseen title, then how many more.
//
//   "Rocks can be reordered, plus 3 more"
//
// Type-only import, so bare Node can load it.
import type { WhatsNewItem } from '../../api/whatsNew';

type SpotlightItem = Pick<WhatsNewItem, 'title'>;

/**
 * The bar's sentence for these unseen updates (newest first, as the feed
 * sends them); null when there are none.
 */
export function spotlightText(items: readonly SpotlightItem[]): string | null {
    const [first, ...rest] = items;
    if (!first) return null;
    if (rest.length === 0) return first.title;
    return `${first.title}, plus ${rest.length} more`;
}
