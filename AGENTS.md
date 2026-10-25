# Issue proposal rationale

For this project, this rule overrides the global issue-reference format. Keep the
mandatory reading requirement, but do not append a generic "Required reading"
list to issues.

At the end of **Proposed behavior and scope**, cite the exact essay points that
materially support the proposed fix. Link each relevant source, identify the
quotation or named section, and explain the concrete decision it supports. For
Grug paraphrases, name the section and label the paraphrase as faithful. Verify
wording before quoting advice.

Use only relevant references; do not force all four essays into every proposal.
Keep the rationale beside the proposal, rather than in a detached reading-list
footer. All other global issue-first, labeling and closure rules still apply.

# MR and release workflow

Label every MR immediately after creation. Use exactly one `type:*` and one
`priority:*` label, normally matching its linked issue, plus relevant `area:*`
labels. Preserve historical and automation labels. Check labels again before merge.

Assign issues and MRs to a meaningful delivery milestone. Keep incomplete parent
features in their own future milestone; an infrastructure step does not complete
the feature. Close a milestone only after its promised scope and release are verified.

Use a repository-linked GitHub Project to organize tracked work. Reuse the existing
project and preserve its fields/items; keep item status consistent with the real
issue/MR state and blockers. Use native parent/sub-issue Relationships for feature
steps and native dependency Relationships for actual blocking prerequisites.
Keep the parent open until all required behavior is delivered, not merely its
first infrastructure step. Record concrete limitations if permissions or API
support prevent an operation; prose links are fallback context, not a claim that
a native relationship or project update succeeded.

Use annotated version tags and GitHub releases for verified delivery checkpoints.
Inspect existing versions, tags and releases first; keep `pyproject.toml`, `uv.lock`
and the tagged `CHANGELOG.md` section consistent. Never move or overwrite a
published version tag. After CI passes, merge, deploy locally and verify the exact
revision, authentication, supervision and protection before tagging it.

Reuse `.github/workflows/release.yml`: it checks the tag/version/changelog, builds
and installs distributions, and publishes the release from the changelog section.
Verify the tag target, workflow result, published assets and deployed revision.
Release notes must describe behavior, validation, schema/rollback requirements and
material unfinished work. Keep credentials, state backups and private source or
account evidence out of release assets. An approval-gated or failed publication
is pending, not a completed release; never bypass environment protections.
