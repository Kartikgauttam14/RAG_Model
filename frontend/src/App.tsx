import { useState } from "react";
import { AdminPage } from "./pages/AdminPage";
import { ChatPage } from "./pages/ChatPage";
import "./styles.css";

export default function App() {
  const [page, setPage] = useState<"chat" | "admin">("chat");

  return (
    <div className="app-frame">
      <nav>
        <span>Mansam AI</span>
        <button onClick={() => setPage("chat")}>Chat</button>
        <button onClick={() => setPage("admin")}>Admin</button>
      </nav>
      {page === "chat" ? <ChatPage /> : <AdminPage />}
    </div>
  );
}
