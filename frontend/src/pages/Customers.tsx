/**
 * Customer X-Ray — the customer search + recent-X-Ray surface (UC-45).
 *
 * This was the seventh tab on Productivity, which filed account work next to a
 * personal inbox, calendar and Slack digest. It is a Sales surface, so it now
 * has its own row in the Sales section of the rail: it is worked against a
 * named company rather than against your own day. Moving it also puts the
 * whole feature on one URL family — `/customers` for the search,
 * `/customers/:companyId/xray` for the brief it opens — so the rail row stays
 * lit through the drill-in. The URL and this file keep the `Customers` name;
 * the row and the page title say "Customer X-Ray", because "Customers" read as
 * a customer list and this is where you run an X-Ray.
 *
 * The body is unchanged (`CustomersColumn`); only its frame moved.
 *
 * The client gate mirrors the tab it replaces. Layout.tsx withholds the row and
 * every endpoint re-checks server-side, so this check only decides what someone
 * who typed the URL sees: an empty state that says why, rather than a search box
 * that 403s on every keystroke.
 */
import React from 'react';
import { Building2 } from 'lucide-react';

import { CustomersColumn } from '../components/briefing/CustomersColumn';
import { useAuth } from '../context/AuthContext';
import { EmptyState, EosPageHeader, PageContainer } from './eos/_shared';

export const Customers: React.FC = () => {
    const { canAccessXray } = useAuth();

    return (
        <PageContainer>
            <EosPageHeader
                icon={<Building2 size={20} />}
                title="Customer X-Ray"
                subtitle="Search a customer, then run Fast Research in seconds or commission Deep Research — churn score, relationship timeline, commercial health and expansion openings."
            />

            {canAccessXray ? (
                <div className="overflow-hidden rounded-2xl border border-gray-200 bg-white shadow-sm" data-tour="customers-search">
                    <CustomersColumn />
                </div>
            ) : (
                <EmptyState
                    title="Customer X-Ray isn't open to your team."
                    hint="An admin opens it to teams in Admin → Feature Access. Ask your Cubie admin if you need an X-Ray on an account."
                />
            )}
        </PageContainer>
    );
};

export default Customers;
