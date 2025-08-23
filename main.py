import React from "react";
import { BrowserRouter as Router, Routes, Route, Link } from "react-router-dom";
import Overview from "./pages/Overview";
import Trades from "./pages/Trades";
import Profile from "./pages/Profile";
import Settings from "./pages/Settings";
import "./index.css";

// Store user_id from Discord OAuth once at login
const urlParams = new URLSearchParams(window.location.search);
const userId = urlParams.get("user_id");
if (userId) {
  localStorage.setItem("user_id", userId);
}

const App = () => {
  return (
    <Router>
      <div className="min-h-screen bg-zinc-950 text-white">
        {/* Navbar */}
        <nav className="flex justify-between items-center bg-zinc-900 p-4 shadow-lg">
          <h1 className="text-xl font-bold text-lime">FUT Trader Dashboard</h1>
          <div className="flex gap-6">
            <Link to="/" className="hover:text-lime">Overview</Link>
            <Link to="/trades" className="hover:text-lime">Trades</Link>
            <Link to="/profile" className="hover:text-lime">Profile</Link>
            <Link to="/settings" className="hover:text-lime">Settings</Link>
          </div>
        </nav>

        {/* Main Routes */}
        <main className="p-6">
          <Routes>
            <Route path="/" element={<Overview />} />
            <Route path="/trades" element={<Trades />} />
            <Route path="/profile" element={<Profile />} />
            <Route path="/settings" element={<Settings />} />
          </Routes>
        </main>
      </div>
    </Router>
  );
};

export default App;