import { createRoot } from "react-dom/client";
import App from "./App";
import "./styles.css";
import "uplot/dist/uPlot.min.css";

createRoot(document.getElementById("root")!).render(<App />);
