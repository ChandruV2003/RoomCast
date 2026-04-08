# RoomCast Telnyx Proxy Worker

This Cloudflare Worker exists only to give Telnyx a hostname it accepts for
inbound voice webhooks.

The worker forwards every request to the live RoomCast Telnyx endpoint:

`https://ntcnas.myftp.org/webcall/telephony/telnyx/k-sk52WkbkIx_fvjEX7nV2eGQGkMRZma/voice`

## Why this exists

Telnyx rejected `ntcnas.myftp.org` as an inbound voice webhook domain. A
`workers.dev` hostname is enough to get a stable public endpoint without buying
another domain.

## Deploy with Cloudflare dashboard

1. Sign in to Cloudflare.
2. Go to `Workers & Pages`.
3. Create a new Worker named `roomcast-telnyx-proxy`.
4. Replace the default worker code with the contents of `src/index.js`.
5. Add Worker variables:
   - `ROOMCAST_TELNYX_TARGET_URL`
   - `https://ntcnas.myftp.org/webcall/telephony/telnyx/k-sk52WkbkIx_fvjEX7nV2eGQGkMRZma/voice`
   - `ROOMCAST_PUBLIC_BASE_URL`
   - `https://ntcnas.myftp.org`
6. Deploy the Worker.
7. Copy the resulting `https://<worker>.workers.dev` URL.

Use that Worker URL in Telnyx as the TeXML webhook URL.

Example:

`https://roomcast-telnyx-proxy.<subdomain>.workers.dev`

## Deploy with Wrangler

If you have Cloudflare auth configured locally:

```bash
cd cloudflare/telnyx-proxy
npx wrangler deploy
```

## Telnyx settings

In the Telnyx TeXML application:

- `Voice Method`: `POST`
- `Webhook URL Method`: `Custom URL`
- `Webhook URL`: your `workers.dev` URL

The worker forwards:

- the root webhook request to the Telnyx voice endpoint
- any follow-up `/webcall/...` requests back to the public RoomCast host

That lets Telnyx fetch the signed phone-audio stream after the PIN succeeds.
