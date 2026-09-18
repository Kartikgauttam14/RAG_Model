import type { Citation } from "../services/api";

export function Sources({ citations }: { citations: Citation[] }) {
  if (!citations.length) return null;
  return (
    <details className="sources">
      <summary>Sources ({citations.length})</summary>
      {citations.map((citation, index) => (
        <article key={citation.chunk_id}>
          <strong>[{index + 1}] {citation.document_name}</strong>
          <span>
            Version {citation.document_version}
            {citation.page ? ` · Page ${citation.page}` : ""}
            {citation.section ? ` · ${citation.section}` : ""}
          </span>
          <p>{citation.excerpt}</p>
        </article>
      ))}
    </details>
  );
}

