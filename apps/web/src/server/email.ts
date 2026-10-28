/** `email` with A–Z mapped to a–z and nothing else, as the auth service matches addresses. Unicode
 *  case folding would map some non-ASCII characters (the Kelvin sign) onto ASCII letters, so one
 *  address could come to match another. */
export function asciiLowercase(email: string): string {
  return email.replace(/[A-Z]/g, (letter) => letter.toLowerCase());
}

/** Whether `email` is a Google service account's: an automation account, never a person. */
export function isServiceAccountEmail(email: string): boolean {
  return /@(?:[^@]*\.)?gserviceaccount\.com$/.test(asciiLowercase(email));
}
