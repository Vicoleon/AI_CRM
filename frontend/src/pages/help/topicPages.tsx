import React from 'react';
import type { LucideIcon } from 'lucide-react';
import {
    AppWindow,
    BarChart3,
    Briefcase,
    CalendarClock,
    ClipboardList,
    Compass,
    Database,
    FileBox,
    FileSignature,
    ListChecks,
    MessageSquare,
    Mic,
    PhoneCall,
    RefreshCw,
    Rocket,
    ScanSearch,
    Settings as SettingsIcon,
    Shield,
    Sparkles,
    Store,
    Sunrise,
    ThumbsUp,
    TrendingUp,
    Unlink,
    UserMinus,
    Users,
} from 'lucide-react';
import { Admins } from './topics/Admins';
import { Assets } from './topics/Assets';
import { CubieForChrome } from './topics/CubieForChrome';
import { Chat } from './topics/Chat';
import { CompanySetup } from './topics/CompanySetup';
import { Crm } from './topics/Crm';
import { CrmSync } from './topics/CrmSync';
import { CustomerXray } from './topics/CustomerXray';
import { DailyBriefing } from './topics/DailyBriefing';
import { Eos } from './topics/Eos';
import { GettingStarted } from './topics/GettingStarted';
import { Handoffs } from './topics/Handoffs';
import { Insights } from './topics/Insights';
import { LooseEnds } from './topics/LooseEnds';
import { MeetingPrep } from './topics/MeetingPrep';
import { MyTodos } from './topics/MyTodos';
import { Productivity } from './topics/Productivity';
import { Proposals } from './topics/Proposals';
import { SalesCalls } from './topics/SalesCalls';
import { SalesCockpit } from './topics/SalesCockpit';
import { SalesRhythm } from './topics/SalesRhythm';
import { ScheduledTasks } from './topics/ScheduledTasks';
import { Settings } from './topics/Settings';
import { SmartTeams } from './topics/SmartTeams';
import { Tips } from './topics/Tips';
import { VoiceCommands } from './topics/VoiceCommands';
import { WhatsNew } from './topics/WhatsNew';

// The rendering half of the Help registry: each topic id's page body and icon.
// helpTopics.ts holds the data and stays a leaf module so Node can test it;
// this file is where the JSX lives. A topic in HELP_TOPICS with no entry here
// has no page to show, which backend/tests/test_help_chat_link.py catches.

export interface TopicPage {
    Page: React.FC;
    Icon: LucideIcon;
}

export const TOPIC_PAGES: Record<string, TopicPage> = {
    'getting-started': { Page: GettingStarted, Icon: Rocket },
    'daily-briefing': { Page: DailyBriefing, Icon: Sunrise },
    tips: { Page: Tips, Icon: ThumbsUp },
    'voice-commands': { Page: VoiceCommands, Icon: Mic },
    'whats-new': { Page: WhatsNew, Icon: Sparkles },
    chat: { Page: Chat, Icon: MessageSquare },
    crm: { Page: Crm, Icon: Database },
    'scheduled-tasks': { Page: ScheduledTasks, Icon: CalendarClock },
    productivity: { Page: Productivity, Icon: Briefcase },
    'my-todos': { Page: MyTodos, Icon: ListChecks },
    'cubie-for-chrome': { Page: CubieForChrome, Icon: AppWindow },
    assets: { Page: Assets, Icon: FileBox },
    'meeting-prep': { Page: MeetingPrep, Icon: ClipboardList },
    handoffs: { Page: Handoffs, Icon: UserMinus },
    insights: { Page: Insights, Icon: BarChart3 },
    'sales-cockpit': { Page: SalesCockpit, Icon: TrendingUp },
    'sales-rhythm': { Page: SalesRhythm, Icon: Sunrise },
    'loose-ends': { Page: LooseEnds, Icon: Unlink },
    'sales-calls': { Page: SalesCalls, Icon: PhoneCall },
    'crm-sync': { Page: CrmSync, Icon: RefreshCw },
    proposals: { Page: Proposals, Icon: FileSignature },
    'customer-xray': { Page: CustomerXray, Icon: ScanSearch },
    eos: { Page: Eos, Icon: Compass },
    'smart-teams': { Page: SmartTeams, Icon: Users },
    settings: { Page: Settings, Icon: SettingsIcon },
    admins: { Page: Admins, Icon: Shield },
    'company-setup': { Page: CompanySetup, Icon: Store },
};

/**
 * Glyph for each Help panel heading, keyed by the heading text the way
 * Layout's SECTION_ICONS is. "My Work" and "Sales" reuse the glyphs the Work
 * panel gives the same headings, so a heading looks the same wherever it is.
 */
export const HELP_GROUP_ICONS: Record<string, React.ReactNode> = {
    'Start here': <Rocket size={14} />,
    'Ask Cubie': <MessageSquare size={14} />,
    'My Work': <Briefcase size={14} />,
    Sales: <TrendingUp size={14} />,
    'EOS & Teams': <Compass size={14} />,
    Account: <SettingsIcon size={14} />,
};
