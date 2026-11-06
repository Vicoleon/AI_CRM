"""Smart Teams — internal work-style profiles, working guides, and team assembly.

The contract that keeps this feature safe, and the reason it is a separate
module from ``rubik_core.people``:

* **Self-given data only.** Every input is something the employee typed into
  the Work Style Interview or a report they chose to upload. Nothing here
  scrapes, infers from public sources, or profiles a colleague behind their
  back. ``rubik_core.people`` (UC-47) is the mirror image — it researches
  *external* meeting attendees from public sources and explicitly refuses to
  profile the company's own staff. Do not blur the two.
* **Advisory only.** Profiles exist to help people collaborate and to staff
  project teams. They never feed performance review, promotion, or
  compensation. Using a personality instrument for employment decisions is a
  legal exposure; team-fit guidance is not.
* **The person publishes, not the system.** A generated guide is a draft until
  its subject approves it, and they can unpublish at any time. Raw scores and
  interview transcripts stay private to the person and support admins even
  after the guide is public.
* **No proprietary instruments.** Item banks here are authored in-house on
  public-domain models, or drawn from the public-domain IPIP. See
  ``instruments/``.
"""
