import type { LucideIcon } from 'lucide-react';
import {
    BookOpenText,
    Coins,
    Database,
    FileSignature,
    LayoutDashboard,
    ListChecks,
    MessageSquareText,
    ShieldCheck,
    Target,
    Users,
} from 'lucide-react';
import type { DashboardIconName } from '../../pages/insights/dashboards';

/** The glyph for each dashboard (`pages/insights/dashboards.ts` names them). */
export const DASHBOARD_ICONS: Record<DashboardIconName, LucideIcon> = {
    overview: LayoutDashboard,
    adoption: Users,
    chat: MessageSquareText,
    priorities: Target,
    followThrough: ListChecks,
    crm: Database,
    proposals: FileSignature,
    spend: Coins,
    knowledge: BookOpenText,
    quality: ShieldCheck,
};
