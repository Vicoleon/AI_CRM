// Every page tour in Cubie, gathered from the per-area files in content/.
//
// One file per area so that adding a tour to one page never touches another
// area's file. A new area file must be listed here, or its tours never run;
// tours.test.ts checks every file in content/ is imported.
import type { PageTour } from './types.ts';
import { tours as account } from './content/account.ts';
import { tours as eos } from './content/eos.ts';
import { tours as home } from './content/home.ts';
import { tours as sales } from './content/sales.ts';
import { tours as salesTools } from './content/salesTools.ts';
import { tours as support } from './content/support.ts';
import { tours as work } from './content/work.ts';

export const ALL_TOURS: readonly PageTour[] = [
    ...home,
    ...work,
    ...sales,
    ...salesTools,
    ...eos,
    ...support,
    ...account,
];
