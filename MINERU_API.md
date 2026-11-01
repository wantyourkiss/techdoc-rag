# Optional MinerU cloud parsing

The knowledge workspace can send **new PDF and DOCX imports** to the official MinerU API. Markdown, text and HTML continue to use the local parser. Existing document snapshots and saved citations are not reparsed on startup.

Set these server-only environment variables before restarting the application:

```dotenv
TECHDOC_DOCUMENT_PARSER=mineru_api
TECHDOC_MINERU_API_TOKEN=
TECHDOC_MINERU_MODEL=vlm
TECHDOC_MINERU_TIMEOUT_S=600
```

Supply a real token through a protected environment file or secret store. Never put it in browser code, a committed `.env`, screenshots or logs. The default parser is `local`; `mineru_api` without a token fails before models or the catalog open. `pipeline` is the other accepted cloud model option.

The upload dialog discloses that PDF and Word files leave this server for MinerU. Application limits remain 8 MiB per file and 200 PDF pages. Word packages are checked for expansion limits before transmission. Cancelling an import prevents local publication, but does not guarantee cancellation of an already submitted cloud job.

## Recovery

The worker requests a signed upload URL, uploads bytes, polls the same batch and reads the structured result. It does not expose the NAS files through a public URL. The API bearer token goes only to `mineru.net`, not to the object storage URL.

Private per-import receipts preserve known batches across timeout, restart and explicit retry. An uncertain task-creation response blocks automatic resubmission. Authentication and definite quota rejections can be explicitly retried after correction; an invalid structured result can be checked again from the same batch/cache after an adapter correction. Expired uploads and provider-declared parse failures require a new import. A successful publication removes temporary cloud receipts and ZIPs. The immutable snapshot keeps the result identity and original source hash.

Only HTTPS object URLs from the documented upload/download hosts are allowed; redirects, private DNS addresses, oversized archives, archive traversal and malformed page positions are rejected. A future provider host change requires a reviewed allowlist update rather than a permissive URL fallback.

## Evidence and limitations

Structured text, formulas and bounded table cells are preserved with page positions and normalized coordinates. Table row/column spans are stored as structure. Provider HTML is never inserted into the browser. Unsupported structures and image relationships are excluded with a visible report. Discarded page regions and empty pages are disclosed.

Extracted text is **machine-generated evidence text**, not human-verified OCR or a proof of complete extraction. A successful parse is not a quality certification. Cloud model weights are not pinned; snapshots include a structured-result digest and adapter identity. Native Office output uses document structure order, without fabricated page numbers or coordinates. Only a provider-converted paged result uses converted-document page numbers; these do not guarantee the original Word layout.

Use the download link to verify important values against the original document. The existing claim review and bounded factual guards still apply, with `answer_semantics_verified=false`.

Official API reference: <https://mineru.net/apiManage/docs>.
