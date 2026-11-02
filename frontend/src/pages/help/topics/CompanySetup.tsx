import React from 'react';
import { SubHeading, TopicLink } from '../kit';

// Help → Company setup. Served at /help/company-setup; Chat cites the same id
// from rubik_core/help/content.py, so a change here travels there in the same PR.

export const CompanySetup: React.FC = () => (
    <>
        <p>
            Cubie works for any company, so it starts out knowing nothing about yours. An admin tells it in{' '}
            <span className="font-medium">Admin → Company setup</span>, under Workspace. The page has three sections, each
            saved on its own, and all three start empty.
        </p>

        <SubHeading id="company-profile">The company profile</SubHeading>
        <p>
            The company&apos;s name, legal name, website and address, and a few sentences about the business: its industry,
            what it sells, who it sells to, how it sells, and its main products or services. Chat, call wrap-ups, CRM Sync,
            Customer X-Ray, Meeting Prep, Loose Ends replies and proposals all read it, so their answers and drafts speak for
            your company. Until it is filled in they simply say &ldquo;the company&rdquo;; a blank field is left out, never
            guessed. The name, legal name, address and website also make up the letterhead of every proposal.
        </p>
        <p>
            The company&apos;s name also heads the other documents Cubie makes for you: Meeting Prep and deep prep briefs
            (the Word download and the emailed PDF), the L10 recap email, and the Scorecard and VTO Excel templates. Until
            the profile has a name or legal name, they carry Cubie&apos;s name instead.
        </p>

        <SubHeading id="company-catalog">The product catalog</SubHeading>
        <p>
            The products and services the company sells, uploaded as a CSV whose first row is{' '}
            <span className="font-mono text-xs">name,description,category,price,unit,url</span>. Only the name is required:
            leave the price blank for anything priced per job, and use the unit for a price such as $90 per hour. Each upload
            replaces the whole catalog. A file with even one bad row changes nothing, and the page lists every row to fix, by
            the row number your spreadsheet shows.
        </p>
        <p>
            <TopicLink id="customer-xray">Customer X-Ray</TopicLink> suggests cross-sell opportunities from the catalog, Meeting
            Prep tells sales what each matched product does, and chat can look products up. With no catalog uploaded none of
            them invents a product.
        </p>

        <SubHeading id="company-templates">Proposal templates</SubHeading>
        <p>
            A template is a standard write-up of one service plus the catalog items that price it. When a call is about that
            service, the <TopicLink id="proposals">Proposal Generator</TopicLink> suggests the template, prints its write-up
            word for word after the scope of work, and adds its catalog prices to the pricing table, where the account
            executive can change them before anything is generated. Prices never come from the AI&apos;s own wording.
        </p>
        <p>
            In the write-up, start a line with # for a heading and - for a bullet. Untick &ldquo;Offer it on new
            proposals&rdquo; to retire a template without deleting it: new proposals stop offering it, and proposals that
            already use it keep printing its write-up. Without any templates, proposals are written from the call alone.
        </p>
    </>
);
