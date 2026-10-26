import { createContext, useContext } from "react";
import type { ReviewApi } from "../types";

const ApiContext = createContext<ReviewApi | null>(null);

export const ApiProvider = ApiContext.Provider;

export function useApi(): ReviewApi {
  const api = useContext(ApiContext);
  if (!api) throw new Error("useApi must be used inside <ApiProvider>");
  return api;
}
