# Knowledge bulk transfer

Select a brand, then use **Knowledge → Import knowledge**. Download the CSV template from the import dialog, choose a UTF-8 CSV or JSON file, and select **Validate and preview**. Preview shows every row's status, errors, warnings, source, restrictions, tags, and retrieval state. It does not create knowledge. Filter issues or duplicates and page through large previews before committing.

The exact CSV columns are:

```text
title,category,body,source,usage_restrictions,tags,verification,available_for_retrieval
```

JSON is an array of objects with the same field names. JSON `tags` is an array of strings and `available_for_retrieval` is a boolean. CSV accepts comma-separated tags, or a JSON array in the tags cell when a tag itself contains a comma. CSV accepts `true`/`false`, `yes`/`no`, or `1`/`0` for retrieval state. Export uses a JSON array in the CSV tags cell so tags round-trip exactly.

Categories must match a current brand pillar or a category already used by that brand. When the brand has no pillars or prior categories, a nonempty category is accepted. Sources and usage restrictions are stored as entered; import never fetches source URLs. Required title and body, field lengths, tags, categories, and retrieval values are checked before commit.

Only explicit `APPROVED` (case insensitive) plus enabled retrieval makes a record eligible for AI. Missing or unrecognized verification becomes `PENDING` with a warning. The existing retrieval rule remains in force.

Duplicate detection compares normalized titles and normalized body hashes against this brand and earlier rows in the file. Choose **Skip duplicates** or **Update matching**; later duplicates within the same file are always skipped. A row matching two different existing records is invalid and must be resolved manually. If invalid rows exist, explicitly acknowledge that they will be rejected before committing valid rows.

A preview is bound to the signed-in session, brand, file contents, and current brand knowledge. It expires after 30 minutes and can be committed once. If the file, pillars, or existing knowledge change, validate again. The commit runs in one database transaction and flushes in batches. Audit entries contain counts and decisions, without copying knowledge bodies.

Use **Knowledge → Export knowledge** to download this brand's full knowledge as CSV or JSON. Both formats include every field needed to reimport: title, category, body, source, restrictions, tags, verification, and retrieval state. Keep exports in an appropriately protected location because they contain the full knowledge body and source text.
