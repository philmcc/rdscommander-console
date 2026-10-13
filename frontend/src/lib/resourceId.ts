/**
 * The id the console uses for a resource in URLs and requests: URL-safe base64 of the ARN without padding,
 * the same encoding the backend applies. Use it only for ARNs that arrive bare; payloads that carry a
 * `resource_id` already hold the right value.
 *
 * @example
 * resourceIdFromArn("arn:aws:rds:eu-west-2:000000000000:db:demo-orders");
 * // "YXJuOmF3czpyZHM6ZXUtd2VzdC0yOjAwMDAwMDAwMDAwMDpkYjpkZW1vLW9yZGVycw"
 */
export function resourceIdFromArn(arn: string): string {
  const bytes = new TextEncoder().encode(arn);
  const binary = Array.from(bytes, (byte) => String.fromCharCode(byte)).join("");
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}
