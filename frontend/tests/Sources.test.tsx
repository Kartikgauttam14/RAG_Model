import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Sources } from "../src/components/Sources";

describe("Sources", () => {
  it("renders real citation provenance", () => {
    render(<Sources citations={[{
      document_id: "doc", document_name: "Guide", document_version: 2,
      chunk_id: "chunk", page: 4, section: "Delivery", excerpt: "Delivery takes two days."
    }]} />);
    expect(screen.getByText("Sources (1)")).toBeInTheDocument();
    expect(screen.getByText(/Version 2 · Page 4 · Delivery/)).toBeInTheDocument();
  });
});
