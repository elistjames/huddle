/**
 * design/components/bundle.js is a classic script that reads `window.React` when it runs
 * (design/docs/USAGE.md, "In a bundler app"). This module must be evaluated before the bundle,
 * so it is imported first by ./design.ts.
 */
import * as React from "react";

declare global {
  interface Window {
    React: typeof React;
  }
}

window.React = React;
