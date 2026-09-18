import { useCallback, useEffect, useState } from "react";
import { getAdminMetrics, listDocuments, uploadDocument } from "../services/api";

export function AdminPage({ token }: { token: string }) {
  const [documents, setDocuments] = useState<Array<{ id: string; name: string; status: string; current_version: number; created_at: string }>>([]);
  const [metrics, setMetrics] = useState<Record<string, number>>({});
  const [status, setStatus] = useState("");

  const refresh = useCallback(async () => {
    const [docs, values] = await Promise.all([listDocuments(token), getAdminMetrics(token)]);
    setDocuments(docs);
    setMetrics(values);
  }, [token]);

  useEffect(() => { void refresh(); }, [refresh]);

  async function upload(file?: File) {
    if (!file) return;
    setStatus("Uploading…");
    try {
      await uploadDocument(token, file);
      setStatus("Queued for indexing");
      await refresh();
    } catch (reason) {
      setStatus(reason instanceof Error ? reason.message : "Upload failed");
    }
  }

  return (
    <main className="admin-shell">
      <header><p className="eyebrow">Knowledge operations</p><h1>Admin</h1></header>
      <section className="metric-grid">
        {Object.entries(metrics).map(([name, value]) => <article key={name}><strong>{value}</strong><span>{name.replaceAll("_", " ")}</span></article>)}
      </section>
      <label className="upload">Upload knowledge source<input type="file" onChange={(event) => void upload(event.target.files?.[0])} /></label>
      <p>{status}</p>
      <section className="document-list">
        <h2>Documents</h2>
        {documents.map((document) => (
          <article key={document.id}><strong>{document.name}</strong><span>v{document.current_version} · {document.status}</span></article>
        ))}
      </section>
    </main>
  );
}

