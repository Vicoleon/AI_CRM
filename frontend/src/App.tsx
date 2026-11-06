import React, { useEffect } from 'react';
import { BrowserRouter, Routes, Route, Navigate, Outlet, useLocation } from 'react-router-dom';
import { GoogleOAuthProvider } from '@react-oauth/google';
import { AuthProvider, useAuth, type FeatureKey } from './context/AuthContext';
import { eosUiGate } from './auth/eosAccess';
import { Login } from './pages/Login';
import { WorkspaceNotSetUp } from './pages/WorkspaceNotSetUp';
import { Chat } from './pages/Chat';
import { Assets } from './pages/Assets';
import { MeetingPrep } from './pages/MeetingPrep';
import { CustomerXRay } from './pages/CustomerXRay';
import { Customers } from './pages/Customers';
import CrmSync from './pages/CrmSync';
import { Settings } from './pages/Settings';
import { Admin } from './pages/Admin';
import { Homefeed } from './pages/Homefeed';
import { Productivity } from './pages/Productivity';
import { MyTodos } from './pages/MyTodos';
import { Help } from './pages/Help';
import { HelpTopic } from './pages/help/HelpTopic';
import Handoffs from './pages/Handoffs';
import { ChromeConnect } from './pages/ChromeConnect';
import { SmartTeams } from './pages/SmartTeams';
import { SmartTeamsInterview } from './pages/SmartTeamsInterview';
import { SmartTeamsGuide } from './pages/SmartTeamsGuide';
import { SmartTeamsSkills } from './pages/SmartTeamsSkills';
import { SmartTeamsFindPeople } from './pages/SmartTeamsFindPeople';
import { SmartTeamsCurateSkills } from './pages/SmartTeamsCurateSkills';
import { SmartTeamsSupport } from './pages/SmartTeamsSupport';
import { SmartTeamsCompare } from './pages/SmartTeamsCompare';
import { SmartTeamsTeamBuilder } from './pages/SmartTeamsTeamBuilder';
import { MyDay } from './pages/sales/MyDay';
import { ManagerView } from './pages/sales/ManagerView';
import { SupervisorWorklist } from './pages/sales/SupervisorWorklist';
import { ExecSummary } from './pages/sales/ExecSummary';
import { LooseEnds } from './pages/sales/LooseEnds';
import { StartCall } from './pages/calls/StartCall';
import { WrapUp } from './pages/calls/WrapUp';
import { Proposal as ProposalPage } from './pages/sales/Proposal';
import { ProposalFromFathom } from './pages/sales/ProposalFromFathom';
import { Proposals } from './pages/sales/Proposals';
import { ScriptEditor } from './pages/scripts/ScriptEditor';
import { Layout } from './components/Layout';
import { NotificationsProvider } from './context/NotificationsContext';
import { TourProvider } from './tours/TourProvider';
import { WhatsNewProvider } from './components/whatsNew/WhatsNewProvider';
import { peekReturnTo, rememberReturnTo } from './components/whatsNew/returnTo';
import { WhatsNewItemLink } from './pages/WhatsNewLink';
import { RadioPlayer } from './components/RadioPlayer';
import { VTO } from './pages/eos/VTO';
import { Scorecard } from './pages/eos/Scorecard';
import { Rocks } from './pages/eos/Rocks';
import { Todos } from './pages/eos/Todos';
import { Issues } from './pages/eos/Issues';
import { Meetings } from './pages/eos/Meetings';
import { L10Session } from './pages/eos/L10Session';
import { Processes } from './pages/eos/Processes';
import { ExecDashboard } from './pages/eos/ExecDashboard';
import { InsightsDashboardPage } from './pages/insights/InsightsDashboardPage';
import { PersonProfile as InsightsPersonProfile } from './pages/insights/PersonProfile';

const GOOGLE_CLIENT_ID = import.meta.env.VITE_GOOGLE_CLIENT_ID || "MOCK_CLIENT_ID";

// Signed out on a What's New link (the morning Slack post's "See it")?
// Remember it, so signing in lands there rather than home.
// Only those links are remembered or followed (components/whatsNew/returnTo.ts).
const RedirectToLogin: React.FC = () => {
  const { pathname, search } = useLocation();
  useEffect(() => {
    rememberReturnTo(pathname, search);
  }, [pathname, search]);
  return <Navigate to="/login" />;
};

const ProtectedRoute: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { token } = useAuth();
  return token ? <>{children}</> : <RedirectToLogin />;
};

const LoadingProfile: React.FC = () => (
  <div className="p-8 text-center text-gray-500">Loading profile...</div>
);

const AdminRoute: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { token, isAdmin, isLoading } = useAuth();
  if (isLoading) return <LoadingProfile />;
  if (!token) return <Navigate to="/login" />;
  if (!isAdmin) return <Navigate to="/" />;
  return <>{children}</>;
};

/* The one guard for every feature-gated route. The SERVER decides who may
   reach a feature (Admin → Feature Access; /api/users/me ships the verdicts,
   with the client-invisible kill switches folded in where a surface has
   nothing to probe) and every router re-checks on each call. This only keeps
   a page the person cannot use out of the URL bar, sending them to `fallback`.

   It waits for the profile before judging: the user object is populated by an
   effect AFTER the first render, so judging eagerly would bounce a valid user
   on a hard refresh — mid-interview, or the Chrome extension opening a page on
   a cold start. Narrower checks a route cannot make (a kill switch it cannot
   see, the manager view's EOS-seat path) are left to the page, which renders
   the server's refusal as its own sentence. Two gates in a row nest. */
const FeatureRoute: React.FC<{
  feature: FeatureKey;
  fallback?: string;
  children: React.ReactNode;
}> = ({ feature, fallback = '/', children }) => {
  const { token, user, hasFeature, isLoading } = useAuth();
  if (!token) return <Navigate to="/login" />;
  if (isLoading || !user) return <LoadingProfile />;
  if (!hasFeature(feature)) return <Navigate to={fallback} replace />;
  return <>{children}</>;
};

const EosRoute: React.FC = () => {
  const { token, user, isLoading } = useAuth();
  const decision = eosUiGate({ token, user, isLoading }).route;
  if (decision === 'login') return <Navigate to="/login" />;
  if (decision === 'loading') return <LoadingProfile />;
  return decision === 'allow' ? <Outlet /> : <Navigate to="/help" replace />;
};

const AppContent: React.FC = () => {
  const { token } = useAuth();

  return (
    <Routes>
      {/* Signed in: back to the What's New link that sent the person here, if
          one did (the link's own route forgets it), else home. */}
      <Route path="/login" element={token ? <Navigate to={peekReturnTo() ?? '/'} replace /> : <Login />} />
      <Route path="/not-set-up" element={token ? <Navigate to="/" replace /> : <WorkspaceNotSetUp />} />
      <Route path="/" element={
        <ProtectedRoute>
          {/* Above Layout so the nav dots and the Chat sidebar share one poll. */}
          <NotificationsProvider>
            {/* Page tours sit around the shell so the account menu can replay
                one and the cards can float above every page. What's New is
                company-wide, so it does not ride on the copilot-only
                notifications poll: its own feed, read by the sidebar button,
                the drawer (mounted in Layout), the Daily Briefing's bar and
                the deep links below. */}
            <TourProvider>
              <WhatsNewProvider>
                <Layout />
              </WhatsNewProvider>
            </TourProvider>
          </NotificationsProvider>
        </ProtectedRoute>
      }>
        {/* Home: the Daily Briefing, which everyone has unless an admin has
            narrowed the copilot. Every other copilot route falls back to "/";
            someone narrowed out lands on Help (ungated), never on an EOS page,
            which a workspace may have switched off. */}
        <Route index element={<FeatureRoute feature="copilot" fallback="/help"><Homefeed /></FeatureRoute>} />
        <Route path="chat" element={<FeatureRoute feature="copilot"><Chat /></FeatureRoute>} />
        {/* Upload + Topics now live inside the Assets page (modal + admin tab). */}
        <Route path="upload" element={<Navigate to="/assets" replace />} />
        <Route path="topics" element={<Navigate to="/assets?tab=topics" replace />} />
        {/* Departments moved into Administration → Admin → Departments.
            Kept as a redirect so existing bookmarks and Help links survive. */}
        <Route path="departments" element={<Navigate to="/admin?tab=departments" replace />} />
        <Route path="assets" element={<FeatureRoute feature="copilot"><Assets /></FeatureRoute>} />
        <Route path="meeting-prep/:eventId" element={<FeatureRoute feature="copilot"><MeetingPrep /></FeatureRoute>} />
        <Route path="productivity" element={<FeatureRoute feature="copilot"><Productivity /></FeatureRoute>} />
        <Route path="todos" element={<FeatureRoute feature="todos"><MyTodos /></FeatureRoute>} />
        {/* UC-45 — Customer X-Ray. The search surface used to be the Customers tab
            on Productivity; it is Sales work, so it now has a row of its own in
            the Sales section and a route of its own, which also puts the search
            and the brief it opens on one URL family. Both are gated on the
            copilot only — the narrower `customer_xray` gate is the SERVER's,
            mirrored in the rail and in the page's own empty state. */}
        <Route path="customers" element={<FeatureRoute feature="copilot"><Customers /></FeatureRoute>} />
        <Route path="customers/:companyId/xray" element={<FeatureRoute feature="copilot"><CustomerXRay /></FeatureRoute>} />
        <Route path="crm-sync" element={<FeatureRoute feature="crm_sync"><CrmSync /></FeatureRoute>} />
        {/* Cubie Sales Cockpit — one feature, four role-scoped views (rep,
            manager, supervisor, exec). The copilot gate mirrors the backend
            mount and nothing more: both routers sit on the copilot-gated
            `core` parent, and the narrower gates
            (`require_cockpit` / `_manager` / `_supervisor` / `_exec`) are the
            SERVER's. Deliberately no second client-side guard per view —
            `COCKPIT_ENABLED` defaults off and the manager gate has a DB-only
            EOS-seat path, so a client gate would bounce people the API is
            willing to serve, and a bounce is worse than a page that explains
            itself. Each screen renders the 403 as an empty state naming whose
            view it is; the SIDEBAR is where a row is withheld (Layout.tsx). */}
        <Route path="sales" element={<FeatureRoute feature="copilot"><MyDay /></FeatureRoute>} />
        <Route path="sales/manager" element={<FeatureRoute feature="copilot"><ManagerView /></FeatureRoute>} />
        <Route path="sales/supervisor" element={<FeatureRoute feature="copilot"><SupervisorWorklist /></FeatureRoute>} />
        <Route path="sales/exec" element={<FeatureRoute feature="copilot"><ExecSummary /></FeatureRoute>} />
        {/* Loose Ends — the AE loop-closure scan (docs/loose_ends/README.md).
            Copilot-gated only, like its siblings: the `loop_closure` roster and
            the LOOP_CLOSURE_ENABLED switch are the router's, rendered here as
            empty states; the sidebar row is where it is withheld. */}
        <Route path="sales/loose-ends" element={<FeatureRoute feature="copilot"><LooseEnds /></FeatureRoute>} />
        {/* Sales call capture. `/calls/start` is now "Today's client calls", read
            from the AE's own calendar: it binds each meeting to its HubSpot company
            and arms transcription on the calendar's OWN Meet space where that is
            possible. Its ad-hoc form still creates a space from scratch, which
            is the only path for a call that is not on the calendar. A
            conference already RUNNING still cannot be armed by anyone — that
            limit did not change, it just stopped being the whole story.
            `:sessionId` is the param name WrapUp.tsx reads; it also accepts
            `:id` and `?session=`, and this is the canonical one. Literal
            segment declared before the parameterised sibling, per the
            route-order rule. */}
        <Route path="calls/start" element={<FeatureRoute feature="copilot"><StartCall /></FeatureRoute>} />
        {/* Call scripts — the per-product checklists the calendar panel renders and
            the wrap-up measures a call against. This is the AUTHORING surface:
            adding a product script is a form entry by the product owners, no
            deploy and no engineering ticket. A script system only an engineer can update is accurate for one
            quarter and wrong forever after.
            The copilot gate and nothing more, mirroring the /calls screens above and
            for the same reason: `backend/routers/scripts.py` carries the real gates
            (`require_ccs` for WHO, `_require_ccs_enabled` for WHETHER, and
            `user_can_access_cockpit_supervisor` on the four mutating endpoints),
            and `CCS_ENABLED` is a server flag the client never receives. A second
            client-side guard would bounce people the API is willing to serve, which
            is worse than a page that explains itself — and this page does: it
            renders the audience refusal, the flag-off state and the read-only
            (non-author) state as three different sentences. The SIDEBAR is where
            the row is withheld (Layout.tsx). Literal segment declared before the
            parameterised sibling below, per the route-order rule. */}
        <Route path="calls/scripts" element={<FeatureRoute feature="copilot"><ScriptEditor /></FeatureRoute>} />
        <Route path="calls/:sessionId/wrapup" element={<FeatureRoute feature="copilot"><WrapUp /></FeatureRoute>} />
        {/* Proposal Generator — the client proposal drafted from a call's wrap-up.
            The copilot gate and nothing more, for the same reason as the /calls
            screens: backend/routers/proposals.py carries the real gates
            (PROPOSALS_ENABLED → 404, the `proposal_generator` roster → 403, and
            per-row ownership), and the page renders each refusal as its own
            sentence. The wrap-up withholds the button (DraftProposalCta) when
            the caller cannot reach it. */}
        {/* The Proposals list — the AE's proposals and the captured calls a
            proposal can be drafted from. Literal segment declared before the
            parameterised siblings below, per the route-order rule. */}
        <Route path="sales/proposals" element={<FeatureRoute feature="copilot"><Proposals /></FeatureRoute>} />
        {/* Entry by Fathom recording id — the Sales Coach's "Continue in Cubie"
            link. Literal segment declared before the parameterised sibling, per
            the route-order rule. */}
        <Route path="sales/proposals/from-fathom/:recordingId" element={<FeatureRoute feature="copilot"><ProposalFromFathom /></FeatureRoute>} />
        <Route path="sales/proposals/:proposalId" element={<FeatureRoute feature="copilot"><ProposalPage /></FeatureRoute>} />
        {/* UNGATED: a successor can sit in any department, and
            every /api/offboarding/* endpoint gates itself (successor / org-wide
            exec / admin). Non-successors just see the empty state. */}
        <Route path="handoffs" element={<Handoffs />} />
        {/* Insights — one page renders every dashboard from the server's
            payload; Overview is the area's root. `people/:email` is the
            literal segment, declared before the `:dashboard` parameter per the
            route-order rule. */}
        <Route path="insights" element={<FeatureRoute feature="insights"><InsightsDashboardPage /></FeatureRoute>} />
        <Route path="insights/people/:email" element={<FeatureRoute feature="insights"><InsightsPersonProfile /></FeatureRoute>} />
        <Route path="insights/:dashboard" element={<FeatureRoute feature="insights"><InsightsDashboardPage /></FeatureRoute>} />
        <Route path="settings" element={<FeatureRoute feature="copilot"><Settings /></FeatureRoute>} />
        {/* Cubie for Chrome: where the extension sends someone to connect this
            browser. Gated on `chrome_extension` alone, not the copilot: the
            router sits outside `core` on purpose, because the roster reaches
            EOS-only teams too. */}
        <Route path="chrome/connect" element={<FeatureRoute feature="chrome_extension"><ChromeConnect /></FeatureRoute>} />
        {/* UNGATED, and it has to stay that way. Help is the fallback
            destination and the left rail shows it to everyone who can log in,
            so gating it on the copilot would hand EOS-only users an icon that
            bounces them, and it is where a person without the copilot lands. The page
            makes no API calls, and its gated topics (X-Ray, CRM Sync and the
            rest) read those gates themselves, so it degrades on its own. Every
            topic is its own page under it; the old /help#<topic> links Chat
            wrote are redirected to /help/<topic> by the Help home. */}
        <Route path="help" element={<Help />} />
        <Route path="help/:topicId" element={<HelpTopic />} />
        {/* What's New deep link — the morning Slack post's "See it". NOT
            copilot-gated: the channel is company-wide. It opens the drawer and
            sends the person home (pages/WhatsNewLink.tsx). */}
        <Route path="whats-new/:itemId" element={<WhatsNewItemLink />} />
        {/* Smart Teams — company-wide, gated on `smart_teams` alone, not the
            copilot: the API serves the interview and your own guide to every
            employee the roster admits. The interview is a
            child route rather than a modal so that `?section=` and `?page=` are
            in the URL: a refresh or a re-login mid-interview has to land back on
            the same section, and there is no other place to keep that. */}
        <Route path="smart-teams">
          <Route index element={<FeatureRoute feature="smart_teams"><SmartTeams /></FeatureRoute>} />
          <Route path="interview" element={<FeatureRoute feature="smart_teams"><SmartTeamsInterview /></FeatureRoute>} />
          {/* Company-wide, same gate as the interview. `guide/:email` is a
              colleague's PUBLISHED guide — company-wide on purpose: the API
              serves it to every employee (the subject's approval IS the
              consent), so copilot-gating the reader would hide a document its
              subject deliberately published. It is the same component as the
              own-guide view; the presence of :email is what switches it. */}
          <Route path="guide" element={<FeatureRoute feature="smart_teams"><SmartTeamsGuide /></FeatureRoute>} />
          <Route path="guide/:email" element={<FeatureRoute feature="smart_teams"><SmartTeamsGuide /></FeatureRoute>} />
          <Route path="skills" element={<FeatureRoute feature="smart_teams"><SmartTeamsSkills /></FeatureRoute>} />
          {/* Curating the shared vocabulary. Support admins only, enforced by
              the server (`can_curate_skill_taxonomy`) — this route carries the
              ordinary Smart Teams gate and the page renders the 403, rather
              than a client-side check pretending to be the authority. */}
          <Route path="skills/curate" element={<FeatureRoute feature="smart_teams"><SmartTeamsCurateSkills /></FeatureRoute>} />
          {/* Support: one person's raw interview, skills, guide rows and
              uploads. Same shape as skills/curate above — the ordinary Smart
              Teams gate on the route, and `can_view_raw_profile` enforced by
              every one of the four reads the page makes. A client-side check
              here would be a second opinion about a rule the server already
              owns, and it would be the WRONG one for the case that matters:
              this route is legitimately reachable by a non-admin looking up
              THEMSELVES, which that predicate allows and a role check would
              not. The subject is in `?email=` so a lookup pastes into a ticket. */}
          <Route path="support" element={<FeatureRoute feature="smart_teams"><SmartTeamsSupport /></FeatureRoute>} />
          {/* Smart Teams AND the copilot (plan.md Decision 4), mirroring how
              the routers stack `require_smart_teams_access` and
              `require_full_app_access`; without the copilot the person goes
              back to /smart-teams, which they CAN use. `compare/:email` is the pair
              guide written TO the caller ABOUT that person; `compare` with no
              email is the colleague picker. */}
          <Route path="compare" element={<FeatureRoute feature="smart_teams"><FeatureRoute feature="copilot" fallback="/smart-teams"><SmartTeamsCompare /></FeatureRoute></FeatureRoute>} />
          <Route path="compare/:email" element={<FeatureRoute feature="smart_teams"><FeatureRoute feature="copilot" fallback="/smart-teams"><SmartTeamsCompare /></FeatureRoute></FeatureRoute>} />
          <Route path="team-builder" element={<FeatureRoute feature="smart_teams"><FeatureRoute feature="copilot" fallback="/smart-teams"><SmartTeamsTeamBuilder /></FeatureRoute></FeatureRoute>} />
          {/* Skills → people. Copilot-gated with Compare and Team Builder for the
              same reason: reading your OWN skills is company-wide, fanning out
              over colleagues is not. */}
          <Route path="find-people" element={<FeatureRoute feature="smart_teams"><FeatureRoute feature="copilot" fallback="/smart-teams"><SmartTeamsFindPeople /></FeatureRoute></FeatureRoute>} />
        </Route>
        <Route path="admin" element={

          <AdminRoute>
            <Admin />
          </AdminRoute>
        } />
        {/* The workspace module switch gates every EOS page, including direct
            bookmarks. The API applies the same switch before tenant DB access. */}
        <Route path="eos" element={<EosRoute />}>
          <Route index element={<Navigate to="meetings" replace />} />
          <Route path="vto" element={<VTO />} />
          <Route path="scorecard" element={<Scorecard />} />
          <Route path="rocks" element={<Rocks />} />
          <Route path="todos" element={<Todos />} />
          <Route path="issues" element={<Issues />} />
          <Route path="meetings" element={<Meetings />} />
          <Route path="meetings/:meetingId" element={<L10Session />} />
          {/* EOS People management removed — people are managed in the
              main system. `eos_people` stays as the ownership backbone. */}
          <Route path="processes" element={<Processes />} />
          {/* Exec-only cross-department rollup. The page itself
              gates on is_exec via useAuth — admins can see it too. */}
          <Route path="dashboard" element={<ExecDashboard />} />
        </Route>
      </Route>
    </Routes>
  );
};

function App() {
  return (
    <GoogleOAuthProvider clientId={GOOGLE_CLIENT_ID}>
      <AuthProvider>
        <BrowserRouter>
          <AppContent />
        </BrowserRouter>
        <RadioPlayer />
      </AuthProvider>
    </GoogleOAuthProvider>
  );
}

export default App;
