// GET  /.netlify/functions/alert-confirm?token=...  -> page with a Confirm button
// POST /.netlify/functions/alert-confirm              -> confirms the alert
// See actionPage in ../lib/alerts.mjs for why a GET never confirms.

import { db, actionPage, readToken, describeAlert } from "../lib/alerts.mjs";

export default async (req) => {
  const token = await readToken(req);
  if (!token) {
    return actionPage({ title: "Link not recognised", message: "That confirmation link isn't valid. Try setting up the alert again." });
  }
  try {
    const [alert] = await db(`alerts?select=*&token=eq.${token}`);
    if (!alert) {
      return actionPage({
        title: "Alert not found",
        message: "This alert no longer exists. Unconfirmed alerts are removed after 48 hours, so you may need to set it up again.",
      });
    }
    if (req.method !== "POST") {
      if (alert.confirmed_at) {
        return actionPage({ title: "Already confirmed", message: `Your alert is on: ${describeAlert(alert)}.` });
      }
      return actionPage({
        title: "Confirm your alert",
        message: `${describeAlert(alert)}.`,
        button: "Confirm my alert",
        action: "/.netlify/functions/alert-confirm",
        token,
      });
    }
    if (!alert.confirmed_at) {
      await db(`alerts?token=eq.${token}`, { method: "PATCH", body: { confirmed_at: new Date().toISOString() } });
    }
    return actionPage({
      title: "Alert confirmed",
      message: "You'll get an email when tee times matching your search come up, usually within a few hours of " +
               "them appearing. Every email has a link to unsubscribe, and the alert ends by itself after 90 days.",
    });
  } catch (e) {
    console.error("alert-confirm:", e.message);
    return actionPage({ title: "Something went wrong", message: "We couldn't confirm your alert just now. Please try the link again later." });
  }
};
