export function readAndClearPin(form: HTMLFormElement): string {
  const pin = form.elements.namedItem("pin");
  if (!(pin instanceof HTMLInputElement)) return "";
  const value = pin.value;
  pin.value = "";
  return value;
}
