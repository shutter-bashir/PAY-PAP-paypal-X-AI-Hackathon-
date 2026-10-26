"use client";

import { AllCommunityModule, ModuleRegistry } from "ag-grid-community";

let registered = false;

export function registerGrid() {
  if (!registered) {
    ModuleRegistry.registerModules([AllCommunityModule]);
    registered = true;
  }
}
