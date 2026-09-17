// POST /.netlify/functions/alert-subscribe
// Saves an alert (unconfirmed) and emails the confirmation link.

import { db, sendEmail, siteUrl, escapeHtml, describeAlert, MAX_ALERTS_PER_EMAIL } from "../lib/alerts.mjs";

const json = (status, body) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const TIME_RE = /^([01]\d|2[0-3]):[0-5]\d$/;

// Everything the browser sends is checked here as well as by the database, so
// a bad request gets a clear message rather than a constraint error.
export function validate(input) {
  const i = input || {};
  const email = String(i.email || "").trim().toLowerCase();
  if (!EMAIL_RE.test(email) || email.length > 254) return { error: "Please enter a valid email address." };
  const lat = Number(i.lat), lng = Number(i.lng);
  if (!(lat >= 49 && lat <= 61 && lng >= -9 && lng <= 3)) return { error: "Search for a UK location first." };
  const radius_km = Number(i.radius_km);
  if (!(radius_km >= 1 && radius_km <= 81)) return { error: "Choose a search radius." };
  const players = Number(i.players);
  if (![1, 2, 3, 4].includes(players)) return { error: "Choose 1 to 4 players." };
  const days = Array.isArray(i.days) ? [...new Set(i.days.map(Number))].filter((d) => d >= 1 && d <= 7) : [];
  const time_from = String(i.time_from || "06:00"), time_to = String(i.time_to || "20:00");
  if (!TIME_RE.test(time_from) || !TIME_RE.test(time_to) || time_from >= time_to) {
    return { error: "The earliest time must be before the latest time." };
  }
  const max_price = i.max_price === null || i.max_price === "" || i.max_price === undefined ? null : Number(i.max_price);
  if (max_price !== null && !(max_price >= 0)) return { error: "Max price must be a number." };
  const holes = i.holes ? Number(i.holes) : null;
  if (holes !== null && ![9, 18].includes(holes)) return { error: "Holes must be 9 or 18." };
  const place = String(i.place || "").slice(0, 80) || null;
  return { alert: { email, place, lat, lng, radius_km, days, time_from, time_to, players, max_price, holes } };
}

export default async (req) => {
  if (req.method !== "POST") return json(405, { error: "POST only" });
  let input;
  try { input = await req.json(); } catch { return json(400, { error: "Bad request." }); }

  const { alert, error } = validate(input);
  if (error) return json(400, { error });

  try {
    // Count this address's live alerts: confirmed ones, and unconfirmed ones
    // from the last 48 hours (older unconfirmed ones are deleted by the sender).
    const since = new Date(Date.now() - 48 * 3600 * 1000).toISOString();
    const existing = await db(
      `alerts?select=id&email=eq.${encodeURIComponent(alert.email)}` +
      `&or=(confirmed_at.not.is.null,created_at.gte.${since})`,
    );
    if (existing.length >= MAX_ALERTS_PER_EMAIL) {
      return json(429, { error: `You already have ${MAX_ALERTS_PER_EMAIL} alerts. Unsubscribe from one to add another.` });
    }

    const [saved] = await db("alerts", { method: "POST", body: alert, prefer: "return=representation" });
    const link = `${siteUrl()}/.netlify/functions/alert-confirm?token=${saved.token}`;
    const summary = describeAlert(saved);
    await sendEmail({
      to: alert.email,
      subject: "Confirm your tee time alert",
      text: `Please confirm your tee time alert:\n\n${summary}\n\nConfirm: ${link}\n\n` +
            `If you didn't ask for this, ignore this email and nothing will be sent.`,
      html: `<p>Please confirm your tee time alert:</p><p><strong>${escapeHtml(summary)}</strong></p>` +
            `<p><a href="${escapeHtml(link)}">Confirm my alert</a></p>` +
            `<p style="color:#5c6b60">If you didn't ask for this, ignore this email and nothing will be sent.</p>`,
    });
    return json(200, { ok: true });
  } catch (e) {
    console.error("alert-subscribe:", e.message);
    return json(500, { error: "Sorry, we couldn't set up that alert. Please try again later." });
  }
};
