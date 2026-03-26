# RoomCast Telephony Setup

RoomCast now exposes inbound phone webhooks for both Twilio and Telnyx.

## Environment

Set these on the RoomCast server:

- `ROOMCAST_TELEPHONY_SECRET`
- `ROOMCAST_TWILIO_WEBHOOK_TOKEN`
- `ROOMCAST_TELNYX_WEBHOOK_TOKEN`

## Twilio

1. Buy or assign a voice-capable phone number in Twilio.
2. Set the incoming voice webhook URL to:

   `https://ntcnas.myftp.org/webcall/telephony/twilio/<ROOMCAST_TWILIO_WEBHOOK_TOKEN>/voice`

3. Use `HTTP POST`.
4. Save the number configuration.

Call flow:

- caller dials the number
- Twilio asks for the four-digit PIN
- RoomCast resolves the active room for that PIN
- Twilio receives a signed telephony WAV stream URL and plays the live room

## Telnyx

1. Buy or assign a voice-capable number in Telnyx.
2. Attach the number to a TeXML application.
3. Set the TeXML application URL to:

   `https://ntcnas.myftp.org/webcall/telephony/telnyx/<ROOMCAST_TELNYX_WEBHOOK_TOKEN>/voice`

4. Use `POST` as the fetch method.
5. Save the TeXML application and number assignment.

Call flow is the same as Twilio:

- caller dials the number
- Telnyx asks for the four-digit PIN
- RoomCast resolves the active room for that PIN
- Telnyx receives a signed telephony WAV stream URL and plays the live room

## Notes

- The phone path uses a telephony-specific WAV stream instead of the browser listener stream.
- The telephony stream is downsampled to mono `8 kHz / 16-bit` WAV for PSTN compatibility.
- The public listener PIN remains the same shared PIN used on the web side.
