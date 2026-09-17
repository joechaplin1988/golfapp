// Shared by the alert functions. Server-side only: this file holds the logic
// that uses the Supabase SERVICE ROLE key, which must never reach a browser.
//
// Environment variables, all set in the Netlify UI (Site configuration ->
// Environment variables), never committed:
//   SUPABASE_URL               https://<ref>.supabase.co
//   SUPABASE_SERVICE_ROLE_KEY  Supabase -> Project settings -> API keys
//   RESEND_API_KEY             from resend.com, once the sending domain is verified
//   ALERTS_FROM                e.g. "Tee Times Near You <alerts@example.com>"
//   SITE_URL                   https://golfbookingapp.netlify.app

export const MAX_ALERTS_PER_EMAIL = 5;

const env = (name) => {
  const v = process.env[name];
  if (!v) throw new Error(`${name} is not set`);
  return v;
};

export const siteUrl = () => (process.env.SITE_URL || "https://golfbookingapp.netlify.app").replace(/\/$/, "");

// Supabase REST with the service role key. RLS on the alerts tables has no
// policies, so this key is the only way in from the web side.
export async function db(path, { method = "GET", body, prefer } = {}) {
  const key = env("SUPABASE_SERVICE_ROLE_KEY");
  const res = await fetch(`${env("SUPABASE_URL")}/rest/v1/${path}`, {
    method,
    headers: {
      apikey: key,
      Authorization: `Bearer ${key}`,
      "Content-Type": "application/json",
      ...(prefer ? { Prefer: prefer } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  if (!res.ok) throw new Error(`database ${method} ${path.split("?")[0]} failed: ${res.status} ${text.slice(0, 200)}`);
  return text ? JSON.parse(text) : null;
}

export async function sendEmail({ to, subject, html, text, headers }) {
  const res = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: { Authorization: `Bearer ${env("RESEND_API_KEY")}`, "Content-Type": "application/json" },
    body: JSON.stringify({ from: env("ALERTS_FROM"), to: [to], subject, html, text, headers }),
  });
  if (!res.ok) throw new Error(`email send failed: ${res.status} ${(await res.text()).slice(0, 200)}`);
}

export const escapeHtml = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export function describeAlert(a) {
  const days = !a.days || a.days.length === 0 || a.days.length === 7
    ? "any day"
    : [...a.days].sort().map((d) => DAY_NAMES[d - 1]).join(", ");
  const miles = Math.round(Number(a.radius_km) * 0.621371);
  const price = a.max_price != null ? `, up to £${Number(a.max_price).toFixed(0)}` : "";
  const holes = a.holes ? `, ${a.holes} holes` : "";
  return `${a.players} player${a.players === 1 ? "" : "s"} within ${miles} miles of ${a.place || "your search"}, ` +
    `${days}, ${String(a.time_from).slice(0, 5)}-${String(a.time_to).slice(0, 5)}${holes}${price}`;
}

// Email clients and work mail scanners open every link in a message to check
// it. If opening a link confirmed or deleted an alert, scanners would do both
// on people's behalf. So every link lands on this page, and only the button,
// which sends a POST, acts.
export function actionPage({ title, message, button, action, token }) {
  const form = button
    ? `<form method="post" action="${escapeHtml(action)}">
         <input type="hidden" name="token" value="${escapeHtml(token)}">
         <button type="submit">${escapeHtml(button)}</button>
       </form>`
    : `<p><a href="${escapeHtml(siteUrl())}/">Back to tee times</a></p>`;
  return new Response(
    `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>${escapeHtml(title)} — Tee Times Near You</title>
<style>
  body{margin:0;font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f6f7f5;color:#1c2620}
  main{max-width:460px;margin:12vh auto;padding:24px;background:#fff;border:1px solid #e2e6e0;border-radius:14px}
  h1{font-size:1.3rem;margin:0 0 8px} p{color:#5c6b60}
  button{font:inherit;font-weight:700;padding:12px 18px;border:0;border-radius:9px;background:#1f6b3b;color:#fff;cursor:pointer;width:100%}
  a{color:#1f6b3b}
</style></head><body><main><h1>${escapeHtml(title)}</h1><p>${escapeHtml(message)}</p>${form}</main></body></html>`,
    { status: 200, headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" } },
  );
}

// A token from a query string (GET) or a posted form / one-click unsubscribe (POST).
export async function readToken(req) {
  const url = new URL(req.url);
  let token = url.searchParams.get("token");
  if (!token && req.method === "POST") {
    const type = req.headers.get("content-type") || "";
    const raw = await req.text();
    if (type.includes("application/json")) {
      try { token = JSON.parse(raw).token; } catch { /* fall through */ }
    } else {
      token = new URLSearchParams(raw).get("token");
    }
  }
  return /^[0-9a-f-]{36}$/i.test(token || "") ? token : null;
}
