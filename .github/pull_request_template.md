## What does this change?

<!-- One or two sentences. The PR title must follow conventional commits
     (feat:, fix:, docs:, ...) because the release changelog is generated
     from it. -->

## Why?

<!-- The problem being solved, not the diff restated. -->

## How was it verified?

- [ ] `pytest` passes locally
- [ ] `npm run test` passes locally
- [ ] `npm run build` succeeds
- [ ] Manually exercised the affected screens/endpoints

<!-- If something could not be verified, say so here rather than leaving it
     implied. -->

## Risk

- [ ] Database schema change (migration required — no Alembic yet, see docs)
- [ ] Changes an API contract the frontend depends on
- [ ] Touches authentication, authorization or upload handling
- [ ] Changes deployment configuration

## Notes for the reviewer

<!-- Anything deliberately left out of scope, known limitations, follow-up work. -->
