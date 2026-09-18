import { FormEvent, useState } from "react";
import { AdminPage } from "./pages/AdminPage";
import { ChatPage } from "./pages/ChatPage";
import { getMe, login, User } from "./services/api";
import "./styles.css";

const AUTH_ENABLED = import.meta.env.VITE_AUTH_ENABLED === "true";
const DEVELOPMENT_USER: User = {
  id: "00000000-0000-0000-0000-000000000000",
  email: "development@localhost",
  role: "admin",
  tenant_id: "default",
};

export default function App() {
  const [token, setToken] = useState(AUTH_ENABLED ? "" : "development-disabled");
  const [user, setUser] = useState<User | undefined>(AUTH_ENABLED ? undefined : DEVELOPMENT_USER);
  const [page, setPage] = useState<"chat" | "admin">("chat");
  const [error, setError] = useState("");

  async function signIn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try {
      const nextToken = await login(String(form.get("email")), String(form.get("password")));
      setToken(nextToken);
      setUser(await getMe(nextToken));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Login failed");
    }
  }

  if (!user || (AUTH_ENABLED && !token)) {
    return (
      <main className="login-shell">
        <form onSubmit={signIn}>
          <p className="eyebrow">Private, grounded assistance</p><h1>Mansam AI</h1>
          <label>Email<input name="email" type="email" required /></label>
          <label>Password<input name="password" type="password" required minLength={12} /></label>
          <button type="submit">Sign in</button>
          {error && <p className="error">{error}</p>}
        </form>
      </main>
    );
  }

  return (
    <div className="app-frame">
      <nav>
        <span>{user.email}</span>
        <button onClick={() => setPage("chat")}>Chat</button>
        {user.role === "admin" && <button onClick={() => setPage("admin")}>Admin</button>}
        <button onClick={() => { setToken(""); setUser(undefined); }}>Sign out</button>
      </nav>
      {page === "chat" ? <ChatPage token={token} /> : <AdminPage token={token} />}
    </div>
  );
}

