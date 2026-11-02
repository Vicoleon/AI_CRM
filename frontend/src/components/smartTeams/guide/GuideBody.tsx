// The guide document, rendered exactly as the server wrote it.
//
// `content_md` is a whole document — its own `# How to Be Successful Working
// with <name>` H1, the deck, the framing, the numbered guidelines, whichever
// descriptive sections survived their confidence gate, the "How complete this
// is" table, and the advisory notice as a closing italic line. So this component
// renders it and adds nothing: no section headers of its own, no placeholder
// cards for the parts a refused layer left out, no summary above it.
//
// WHY NOTHING IS PADDED
// ---------------------
// `rubik_core/smart_teams/guide.py` omits a section whose layer is refused
// rather than substituting a hedge — the module says so at length, and the one
// place a shortfall IS named is a section the server writes itself
// ("Not enough to write from yet"). A browser that noticed a missing "Growth
// edge" heading and drew an empty box for it would be inventing an absence the
// document deliberately does not claim. The honest render of a short guide is a
// short guide.
//
// The one thing added is FRAMING for the whole-document shortfall case, and it
// is framing rather than content: a link back to the interview, next to the
// server's own explanation of why the guide is thin.
import React from 'react';
import { ClipboardList, FileWarning } from 'lucide-react';
import { Link } from 'react-router-dom';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

/** The framing for a guide that is mostly hedges.
 *
 *  Deliberately not an error and not a warning: the server calls this "the
 *  honest outcome rather than a failure", and the document itself lists the
 *  sections that would change it under "How complete this is". This points at
 *  those and offers the way to act on them. */
export const ShortfallFraming: React.FC<{ ownGuide: boolean; displayName?: string }> = ({
    ownGuide,
    displayName,
}) => (
    <div className="mb-4 rounded-lg border border-gray-200 bg-gray-50 p-4">
        <h3 className="flex items-center gap-2 text-sm font-semibold text-gray-800">
            <ClipboardList size={16} className="text-gray-500" />
            {ownGuide
                ? 'Your interview has not given Cubie much to write from yet'
                : 'This is a short guide'}
        </h3>
        {ownGuide ? (
            <>
                <p className="mt-2 text-sm leading-relaxed text-gray-700">
                    Cubie would rather write nothing than describe somebody who is not you, so
                    it has written what your answers support and no more. The
                    &ldquo;How complete this is&rdquo; list below names the exact sections that
                    would fill it in.
                </p>
                <div className="mt-3">
                    <Link
                        to="/smart-teams/interview"
                        className="inline-flex items-center gap-1.5 text-sm font-medium text-brand hover:underline"
                    >
                        <ClipboardList size={14} />
                        Go back to the interview
                    </Link>
                </div>
            </>
        ) : (
            <p className="mt-2 text-sm leading-relaxed text-gray-700">
                {displayName ? `${displayName} has` : 'They have'} published what their
                interview answers support. A short guide is not a small profile — it means
                Cubie declined to write past the evidence.
            </p>
        )}
    </div>
);

/** The document body. `prose` classes match the other markdown surfaces in the
 *  app (Meeting Prep, Customer X-Ray), so a guide reads the same everywhere. */
export const GuideBody: React.FC<{ markdown?: string | null }> = ({ markdown }) => {
    const body = (markdown ?? '').trim();

    // A `ready` row always has a body — `guide.approve` refuses to publish one
    // without it. So this branch is a wire fault, and it says that rather than
    // rendering an empty card that reads as a guide with nothing in it.
    if (!body) {
        return (
            <div className="flex items-start gap-2.5 rounded-lg border border-amber-200 bg-amber-50 p-4">
                <FileWarning className="mt-0.5 flex-shrink-0 text-amber-600" size={18} />
                <div>
                    <p className="text-sm font-medium text-amber-900">
                        The guide came back without its text.
                    </p>
                    <p className="mt-1 text-xs leading-relaxed text-amber-800">
                        Cubie has a record of this guide but did not send its wording. Nothing
                        is lost and nothing has been published — reload the page, and tell
                        an admin if it keeps happening.
                    </p>
                </div>
            </div>
        );
    }

    return (
        <article className="prose prose-sm prose-slate max-w-none leading-relaxed [&_blockquote]:border-l-brand [&_blockquote]:not-italic [&_h1]:mb-2 [&_h1]:text-xl [&_h2]:mt-6 [&_h2]:text-base [&_h3]:text-sm [&_li_p]:my-0 [&_p]:my-2 [&_ul]:space-y-1">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{body}</ReactMarkdown>
        </article>
    );
};

export default GuideBody;
