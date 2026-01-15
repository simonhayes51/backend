import { useState, useEffect } from "react";
import { X, TrendingUp, TrendingDown, Search } from "lucide-react";
import { GradientButton } from "../ui/GradientButton";
import toast from "react-hot-toast";

const API_BASE = import.meta.env.VITE_API_URL || "";
const buildProxy = (url) => `${API_BASE}/img?url=${encodeURIComponent(url)}`;
const PLACEHOLDER = "/img/card-placeholder.png";

const normalizeForSearch = (value = "") =>
  value.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase().trim();

const searchPlayers = async (query) => {
  if (!query.trim()) return [];
  const qNorm = normalizeForSearch(query);
  try {
    const response = await fetch(
      `${API_BASE}/api/search-players?q=${encodeURIComponent(query)}&q_norm=${encodeURIComponent(qNorm)}`,
      { credentials: "include" }
    );
    if (!response.ok) return [];
    const data = await response.json();
    return data.players || [];
  } catch (error) {
    console.error("Search failed:", error);
    return [];
  }
};

export function TradeSignalModal({ isOpen, onClose, onSubmit }) {
  const [signalData, setSignalData] = useState({
    player_name: "",
    buy_price: "",
    sell_price: "",
    signal_type: "buy",
    confidence: "medium",
    notes: "",
  });

  const [searching, setSearching] = useState(false);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState([]);
  const [showResults, setShowResults] = useState(false);
  const [selectedPlayer, setSelectedPlayer] = useState(null);

  useEffect(() => {
    const timeout = setTimeout(async () => {
      if (!query.trim()) {
        setResults([]);
        setShowResults(false);
        return;
      }
      setSearching(true);
      const players = await searchPlayers(query);
      setResults(players);
      setSearching(false);
      setShowResults(true);
    }, 300);

    return () => clearTimeout(timeout);
  }, [query]);

  const handleSubmit = async () => {
    if (!signalData.player_name.trim()) {
      toast.error("Please enter a player name");
      return;
    }
    if (!signalData.buy_price && !signalData.sell_price) {
      toast.error("Please enter at least one price");
      return;
    }

    try {
      const confidenceMap = { low: 33, medium: 66, high: 90 };
      
      await onSubmit({
        post_type: signalData.signal_type === "buy" ? "quick_flip" : "prediction",
        content: signalData.notes || `${signalData.signal_type === "buy" ? "Buy" : "Sell"} signal for ${signalData.player_name}`,
        player_name: signalData.player_name,
        buy_range_min: signalData.buy_price ? parseInt(signalData.buy_price) : undefined,
        buy_range_max: signalData.buy_price ? parseInt(signalData.buy_price) : undefined,
        sell_target: signalData.sell_price ? parseInt(signalData.sell_price) : undefined,
        confidence_level: confidenceMap[signalData.confidence] || 66,
        player_image_url: selectedPlayer?.image_url || undefined,
        card_image_url: selectedPlayer?.card_image_url || undefined,
      });
      
      toast.success("Trade signal posted!");
      setSignalData({
        player_name: "",
        buy_price: "",
        sell_price: "",
        signal_type: "buy",
        confidence: "medium",
        notes: "",
      });
      setQuery("");
      setSelectedPlayer(null);
      onClose();
    } catch (error) {
      console.error("Failed to post signal:", error);
      toast.error(error.userMessage || "Failed to post signal");
    }
  };

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 bg-black/50 backdrop-blur-sm z-50 flex items-center justify-center p-4">
      <div className="bg-card border border-border rounded-2xl max-w-2xl w-full max-h-[90vh] overflow-y-auto">
        {/* Header */}
        <div className="flex items-center justify-between p-6 border-b border-border">
          <h2 className="text-xl font-bold text-foreground">Create Trade Signal</h2>
          <button
            onClick={onClose}
            className="p-2 hover:bg-muted rounded-lg transition-colors"
          >
            <X className="w-5 h-5 text-muted-foreground" />
          </button>
        </div>

        {/* Content */}
        <div className="p-6 space-y-6">
          {/* Signal Type */}
          <div>
            <label className="block text-sm font-medium text-foreground mb-2">
              Signal Type
            </label>
            <div className="flex gap-3">
              <button
                onClick={() => setSignalData({ ...signalData, signal_type: "buy" })}
                className={`flex-1 flex items-center justify-center gap-2 p-3 rounded-lg border transition-all ${
                  signalData.signal_type === "buy"
                    ? "bg-success/10 border-success text-success"
                    : "border-border text-muted-foreground hover:bg-muted"
                }`}
              >
                <TrendingUp className="w-5 h-5" />
                Buy Signal
              </button>
              <button
                onClick={() => setSignalData({ ...signalData, signal_type: "sell" })}
                className={`flex-1 flex items-center justify-center gap-2 p-3 rounded-lg border transition-all ${
                  signalData.signal_type === "sell"
                    ? "bg-destructive/10 border-destructive text-destructive"
                    : "border-border text-muted-foreground hover:bg-muted"
                }`}
              >
                <TrendingDown className="w-5 h-5" />
                Sell Signal
              </button>
            </div>
          </div>

          {/* Player Search */}
          <div>
            <label className="block text-sm font-medium text-foreground mb-2">
              Player Name
            </label>
            <div className="relative">
              <input
                type="text"
                placeholder="Search for a player..."
                value={query}
                onChange={(e) => {
                  const value = e.target.value;
                  setQuery(value);
                  setSignalData({ ...signalData, player_name: value });
                  setSelectedPlayer(null);
                }}
                onFocus={() => {
                  if (results.length > 0) {
                    setShowResults(true);
                  }
                }}
                className="w-full bg-muted/50 border border-border rounded-lg px-4 py-2.5 pr-10 text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
              />
              {searching ? (
                <div className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground">
                  <span className="block h-4 w-4 animate-spin rounded-full border-2 border-muted-foreground/30 border-t-muted-foreground" />
                </div>
              ) : (
                <Search className="absolute right-3 top-1/2 -translate-y-1/2 w-5 h-5 text-muted-foreground" />
              )}
              {showResults && results.length > 0 && (
                <div className="absolute z-10 mt-2 w-full overflow-hidden rounded-lg border border-border bg-card shadow-lg max-h-64 overflow-y-auto">
                  {results.map((player) => (
                    <button
                      key={`${player.card_id || player.id}-${player.rating}`}
                      type="button"
                      className="flex w-full items-center gap-3 border-b border-border/60 px-4 py-2 text-left hover:bg-muted/70 last:border-b-0"
                      onClick={() => {
                        setSignalData({ ...signalData, player_name: player.name });
                        setQuery(player.name);
                        setSelectedPlayer(player);
                        setShowResults(false);
                      }}
                    >
                      <img
                        src={player.image_url || PLACEHOLDER}
                        alt={`${player.name} (${player.rating || "N/A"})`}
                        loading="lazy"
                        className="h-12 w-10 object-contain"
                        onError={(e) => {
                          const img = e.currentTarget;
                          if (!img.dataset.triedProxy && player.image_url) {
                            img.dataset.triedProxy = "1";
                            img.src = buildProxy(player.image_url);
                          } else {
                            img.src = PLACEHOLDER;
                          }
                        }}
                        referrerPolicy="no-referrer"
                      />
                      <div className="flex-1 min-w-0">
                        <div className="truncate font-medium text-foreground">
                          {player.name}
                        </div>
                        <div className="text-xs text-muted-foreground">
                          {player.rating ? `Rating ${player.rating}` : "Rating N/A"}
                        </div>
                      </div>
                    </button>
                  ))}
                </div>
              )}
              {showResults && !searching && query && results.length === 0 && (
                <div className="absolute z-10 mt-2 w-full rounded-lg border border-border bg-card px-4 py-3 text-sm text-muted-foreground shadow-lg">
                  No players found for "{query}"
                </div>
              )}
            </div>
            {selectedPlayer && (
              <div className="mt-3 flex items-center gap-3 rounded-lg border border-border bg-muted/40 px-3 py-2">
                <img
                  src={selectedPlayer.image_url || PLACEHOLDER}
                  alt={`${selectedPlayer.name} (${selectedPlayer.rating || "N/A"})`}
                  className="h-14 w-12 object-contain"
                  onError={(e) => {
                    const img = e.currentTarget;
                    if (!img.dataset.triedProxy && selectedPlayer.image_url) {
                      img.dataset.triedProxy = "1";
                      img.src = buildProxy(selectedPlayer.image_url);
                    } else {
                      img.src = PLACEHOLDER;
                    }
                  }}
                  referrerPolicy="no-referrer"
                />
                <div className="min-w-0">
                  <div className="truncate text-sm font-semibold text-foreground">
                    {selectedPlayer.name}
                  </div>
                  <div className="text-xs text-muted-foreground">
                    {selectedPlayer.rating ? `Rating ${selectedPlayer.rating}` : "Rating N/A"}
                  </div>
                </div>
              </div>
            )}
          </div>

          {/* Price Inputs */}
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-foreground mb-2">
                Buy Price
              </label>
              <div className="relative">
                <span className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground">
                  $
                </span>
                <input
                  type="number"
                  placeholder="0"
                  value={signalData.buy_price}
                  onChange={(e) => setSignalData({ ...signalData, buy_price: e.target.value })}
                  className="w-full bg-muted/50 border border-border rounded-lg pl-8 pr-4 py-2.5 text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
                />
              </div>
            </div>
            <div>
              <label className="block text-sm font-medium text-foreground mb-2">
                Sell Price
              </label>
              <div className="relative">
                <span className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground">
                  $
                </span>
                <input
                  type="number"
                  placeholder="0"
                  value={signalData.sell_price}
                  onChange={(e) => setSignalData({ ...signalData, sell_price: e.target.value })}
                  className="w-full bg-muted/50 border border-border rounded-lg pl-8 pr-4 py-2.5 text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
                />
              </div>
            </div>
          </div>

          {/* Confidence Level */}
          <div>
            <label className="block text-sm font-medium text-foreground mb-2">
              Confidence Level
            </label>
            <div className="flex gap-2">
              {["low", "medium", "high"].map((level) => (
                <button
                  key={level}
                  onClick={() => setSignalData({ ...signalData, confidence: level })}
                  className={`flex-1 px-4 py-2 rounded-lg border capitalize transition-all ${
                    signalData.confidence === level
                      ? "bg-primary/10 border-primary text-primary"
                      : "border-border text-muted-foreground hover:bg-muted"
                  }`}
                >
                  {level}
                </button>
              ))}
            </div>
          </div>

          {/* Notes */}
          <div>
            <label className="block text-sm font-medium text-foreground mb-2">
              Notes (Optional)
            </label>
            <textarea
              placeholder="Add analysis, reasoning, or tips..."
              value={signalData.notes}
              onChange={(e) => setSignalData({ ...signalData, notes: e.target.value })}
              rows={4}
              className="w-full bg-muted/50 border border-border rounded-lg px-4 py-2.5 text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 resize-none"
            />
          </div>
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end gap-3 p-6 border-t border-border">
          <button
            onClick={onClose}
            className="px-4 py-2 text-muted-foreground hover:text-foreground transition-colors"
          >
            Cancel
          </button>
          <GradientButton onClick={handleSubmit}>
            Post Signal
          </GradientButton>
        </div>
      </div>
    </div>
  );
}
