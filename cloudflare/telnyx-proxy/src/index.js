/**
 * Tiny Cloudflare Worker to give Telnyx a stable public hostname that forwards
 * directly to the RoomCast telephony endpoint on ntcnas.myftp.org.
 */

export default {
  async fetch(request, env) {
    const target = new URL(env.ROOMCAST_TELNYX_TARGET_URL);
    const incoming = new URL(request.url);

    target.search = incoming.search;

    const headers = new Headers(request.headers);
    headers.set("host", target.host);
    headers.set("x-forwarded-host", incoming.host);
    headers.set("x-forwarded-proto", incoming.protocol.replace(":", ""));
    headers.set("x-forwarded-for", request.headers.get("cf-connecting-ip") || request.headers.get("x-forwarded-for") || "");

    const init = {
      method: request.method,
      headers,
      redirect: "follow",
      body: request.method === "GET" || request.method === "HEAD" ? undefined : await request.arrayBuffer(),
    };

    return fetch(target, init);
  },
};
