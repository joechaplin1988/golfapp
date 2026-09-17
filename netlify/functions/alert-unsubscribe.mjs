// GET  /.netlify/functions/alert-unsubscribe?token=...  -> page with an Unsubscribe button
// POST /.netlify/functions/alert-unsubscribe              -> deletes the alert
//
// POST also serves one-click unsubscribe from mail clients (RFC 8058): the
// alert emails carry List-Unsubscribe and List-Unsubscribe-Post headers
// pointing here, and Gmail or Apple Mail POSTs "List-Unsubscribe=One-Click"
// with the token still in the query string.
//
// Unsubscribing deletes the alert row, which removes the email address and,
// by cascade, the record of what was sent. Nothing is kept.

import { db, actionPage, readToken } from "../lib/alerts.mjs";

export default async (req) => {
  const token = await readToken(req);
  if (!token) {
    return actionPage({ title: "Link not recognised", message: "That unsubscribe link isn't valid." });
  }
  try {
    if (req.method !== "POST") {
      const [alert] = await db(`alerts?select=id&token=eq.${token}`);
      if (!alert) return actionPage({ title: "Already unsubscribed", message: "This alert no longer exists, so you won't get any more emails about it." });
      return actionPage({
        title: "Unsubscribe",
        message: "Stop this tee time alert and delete your email address from it?",
        button: "Unsubscribe",
        action: "/.netlify/functions/alert-unsubscribe",
        token,
      });
    }
    await db(`alerts?token=eq.${token}`, { method: "DELETE" });
    return actionPage({ title: "Unsubscribed", message: "Your alert is deleted, along with your email address. You won't hear from us about it again." });
  } catch (e) {
    console.error("alert-unsubscribe:", e.message);
    return actionPage({ title: "Something went wrong", message: "We couldn't unsubscribe you just now. Please try the link again later." });
  }
};
