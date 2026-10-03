import { describe, expect, it } from "vitest";
import { splitBold } from "./whatsappText";

describe("splitBold", () => {
  it("makes *text* bold and keeps the rest", () => {
    expect(splitBold("السعر *687.70 ريال* للّيلة")).toEqual([
      { text: "السعر ", bold: false },
      { text: "687.70 ريال", bold: true },
      { text: " للّيلة", bold: false },
    ]);
  });

  it("handles several bold parts and one at either end", () => {
    expect(splitBold("*أ* و *ب*")).toEqual([
      { text: "أ", bold: true },
      { text: " و ", bold: false },
      { text: "ب", bold: true },
    ]);
  });

  it("leaves plain text, an empty text and a lone asterisk alone", () => {
    expect(splitBold("hello")).toEqual([{ text: "hello", bold: false }]);
    expect(splitBold("")).toEqual([]);
    expect(splitBold("a * b")).toEqual([{ text: "a * b", bold: false }]);
    expect(splitBold("**")).toEqual([{ text: "**", bold: false }]);
  });

  it("does not bold across spaces inside the markers, inside a word, or across lines", () => {
    expect(splitBold("* x *")).toEqual([{ text: "* x *", bold: false }]);
    expect(splitBold("2*3*4")).toEqual([{ text: "2*3*4", bold: false }]);
    expect(splitBold("*a\nb*")).toEqual([{ text: "*a\nb*", bold: false }]);
  });

  it("never turns text into markup", () => {
    expect(splitBold("*<b>x</b>*")).toEqual([{ text: "<b>x</b>", bold: true }]);
  });
});
