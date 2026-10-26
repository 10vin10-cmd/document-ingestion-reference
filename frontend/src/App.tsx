import { useEffect, useState } from "react";
import { useSession } from "./auth/session";
import { ReviewQueue } from "./components/ReviewQueue";
import { ReviewScreen } from "./components/ReviewScreen";

/** The open document lives in the URL (?doc=...), so links and Back work. */
function useOpenDocument(): [string | null, (id: string | null) => void] {
  const read = () => new URLSearchParams(window.location.search).get("doc");
  const [id, setId] = useState<string | null>(read);

  useEffect(() => {
    const onPop = () => setId(read());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const navigate = (next: string | null) => {
    const url = new URL(window.location.href);
    if (next) url.searchParams.set("doc", next);
    else url.searchParams.delete("doc");
    window.history.pushState({}, "", url);
    setId(next);
  };
  return [id, navigate];
}

export function App({ demo = false }: { demo?: boolean }) {
  const session = useSession();
  const [openId, setOpenId] = useOpenDocument();
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(null), 5000);
    return () => clearTimeout(timer);
  }, [notice]);

  return (
    <>
      <header className="topbar">
        <a className="brand" href="./" onClick={(e) => { e.preventDefault(); setOpenId(null); }}>
          <img src="./favicon.svg" alt="" width={24} height={24} />
          Invoice Review
        </a>
        {demo && <span className="tag tag-demo">Demo data</span>}
        <div className="user">
          <span className="muted">{session.displayName}</span>
          <button className="btn btn-small" onClick={session.signOut}>Sign out</button>
        </div>
      </header>

      <main className="content">
        {notice && <p className="toast" role="status">{notice}</p>}
        {openId ? (
          <ReviewScreen
            id={openId}
            onClose={(message) => {
              setOpenId(null);
              if (message) setNotice(message);
            }}
          />
        ) : (
          <ReviewQueue onOpen={(id) => { setNotice(null); setOpenId(id); }} />
        )}
      </main>
    </>
  );
}
