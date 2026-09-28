import "@testing-library/jest-dom/vitest";

// jsdom does not implement scrolling APIs used by the live transcript.
if (!Element.prototype.scrollTo) {
  Element.prototype.scrollTo = function scrollTo() {} as typeof Element.prototype.scrollTo;
}
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = function scrollIntoView() {};
}
