import React, { useState, useRef, useEffect } from 'react';
import { Play, Pause, Volume2, VolumeX, Music, ChevronUp, ChevronDown, Minimize2 } from 'lucide-react';
import { motion, AnimatePresence } from 'framer-motion';

type Station = { id: string; name: string; description: string; url: string };

const STATIONS: Station[] = [
  { id: 'seventies', name: 'Left Coast 70s', description: 'Mellow album rock from the 70s', url: 'https://ice1.somafm.com/seventies-128-mp3' },
  { id: 'bootliquor', name: 'Boot Liquor', description: 'Americana roots music', url: 'https://ice1.somafm.com/bootliquor-128-mp3' },
  { id: 'u80s', name: 'Underground 80s', description: 'Classic 80s alternative', url: 'https://ice1.somafm.com/u80s-128-mp3' },
  { id: 'secretagent', name: 'Secret Agent', description: 'Lounge & spy jazz', url: 'https://ice1.somafm.com/secretagent-128-mp3' },
  { id: 'illstreet', name: 'Illinois Street Lounge', description: 'Classic bachelor-pad lounge', url: 'https://ice1.somafm.com/illstreet-128-mp3' },
  { id: 'lush', name: 'Lush', description: 'Mellow female vocals', url: 'https://ice1.somafm.com/lush-128-mp3' },
  { id: 'gsclassic', name: 'Groove Salad Classic', description: 'Mellower downtempo', url: 'https://ice1.somafm.com/gsclassic-128-mp3' },
];

const DEFAULT_STATION_ID = 'seventies';

export const RadioPlayer: React.FC = () => {
  const [isPlaying, setIsPlaying] = useState(false);
  const [volume, setVolume] = useState(() => {
    const saved = localStorage.getItem('radio-volume');
    return saved ? parseFloat(saved) : 0.5;
  });
  const [isMuted, setIsMuted] = useState(false);
  const [isExpanded, setIsExpanded] = useState(false);
  const [isMinimized, setIsMinimized] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [stationId, setStationId] = useState<string>(() => {
    const saved = localStorage.getItem('radio-station');
    if (saved && STATIONS.some(s => s.id === saved)) return saved;
    return DEFAULT_STATION_ID;
  });

  const currentStation = STATIONS.find(s => s.id === stationId) ?? STATIONS[0];

  const audioRef = useRef<HTMLAudioElement | null>(null);
  const autoMinimizeTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const resetAutoMinimizeTimer = () => {
    if (autoMinimizeTimeoutRef.current) {
      clearTimeout(autoMinimizeTimeoutRef.current);
    }
    if (!isMinimized) {
      autoMinimizeTimeoutRef.current = setTimeout(() => {
        setIsMinimized(true);
        setIsExpanded(false); // Also close volume panel if open
      }, 5000);
    }
  };

  useEffect(() => {
    resetAutoMinimizeTimer();
    return () => {
      if (autoMinimizeTimeoutRef.current) clearTimeout(autoMinimizeTimeoutRef.current);
    };
  }, [isMinimized]);

  useEffect(() => {
    // Sync volume to audio element
    if (audioRef.current) {
      audioRef.current.volume = isMuted ? 0 : volume;
    }
    localStorage.setItem('radio-volume', volume.toString());
  }, [volume, isMuted]);

  const togglePlay = () => {
    resetAutoMinimizeTimer();
    if (!audioRef.current) return;

    if (isPlaying) {
      audioRef.current.pause();
      setIsPlaying(false);
    } else {
      setIsLoading(true);
      audioRef.current.play()
        .then(() => {
          setIsPlaying(true);
          setIsLoading(false);
        })
        .catch((err) => {
          console.error("Playback failed:", err);
          setIsLoading(false);
        });
    }
  };

  const handleVolumeChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    resetAutoMinimizeTimer();
    const val = parseFloat(e.target.value);
    setVolume(val);
    if (val > 0) setIsMuted(false);
  };

  const toggleMute = () => {
    resetAutoMinimizeTimer();
    setIsMuted(!isMuted);
  };

  const selectStation = (id: string) => {
    resetAutoMinimizeTimer();
    if (id === stationId) return;
    const next = STATIONS.find(s => s.id === id);
    if (!next || !audioRef.current) return;
    const wasPlaying = isPlaying;
    setStationId(id);
    localStorage.setItem('radio-station', id);
    audioRef.current.src = next.url;
    audioRef.current.load();
    if (wasPlaying) {
      setIsLoading(true);
      audioRef.current.play()
        .then(() => {
          setIsPlaying(true);
          setIsLoading(false);
        })
        .catch((err) => {
          console.error("Playback failed:", err);
          setIsLoading(false);
        });
    }
  };

  return (
    <div className="fixed bottom-6 right-6 z-[9999] flex flex-col items-end gap-3 pointer-events-none">
      <audio
        ref={audioRef}
        src={currentStation.url}
        preload="none"
        onCanPlay={() => setIsLoading(false)}
        onWaiting={() => setIsLoading(true)}
        onPlaying={() => setIsLoading(false)}
      />

      <AnimatePresence>
        {isExpanded && !isMinimized && (
          <motion.div
            initial={{ opacity: 0, y: 10, scale: 0.95, x: 20 }}
            animate={{ opacity: 1, y: 0, scale: 1, x: 0 }}
            exit={{ opacity: 0, y: 10, scale: 0.95, x: 20 }}
            className="pointer-events-auto bg-white/80 backdrop-blur-md border border-white/20 shadow-2xl rounded-2xl p-4 flex flex-col gap-4 w-64 overflow-hidden"
          >
            <div className="flex items-center gap-3">
              <div className={`w-10 h-10 rounded-xl bg-gradient-to-br from-brand to-brand-hover flex items-center justify-center shadow-lg transition-all duration-500 ${isPlaying ? 'animate-pulse' : ''}`}>
                <Music className="text-white" size={20} />
              </div>
              <div className="flex-1 min-w-0">
                <p className="text-xs font-bold text-gray-900 truncate uppercase tracking-wider">SomaFM</p>
                <p className="text-[10px] text-gray-500 truncate">{currentStation.name}</p>
              </div>
            </div>

            <div className="space-y-2">
              <span className="text-[10px] font-semibold text-gray-400 uppercase">Station</span>
              <div className="max-h-[200px] overflow-y-auto -mx-1 px-1 space-y-1">
                {STATIONS.map((station) => {
                  const isSelected = station.id === stationId;
                  return (
                    <button
                      key={station.id}
                      onClick={() => selectStation(station.id)}
                      className={`w-full text-left px-3 py-2 rounded-lg transition-colors ${
                        isSelected
                          ? 'bg-brand text-white'
                          : 'text-gray-700 hover:bg-gray-100'
                      }`}
                    >
                      <div className="text-xs font-semibold truncate">{station.name}</div>
                      <div className={`text-[10px] truncate ${isSelected ? 'text-white/80' : 'text-gray-500'}`}>
                        {station.description}
                      </div>
                    </button>
                  );
                })}
              </div>
            </div>

            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <span className="text-[10px] font-semibold text-gray-400 uppercase">Volume</span>
                <span className="text-[10px] font-bold text-brand">{Math.round(volume * 100)}%</span>
              </div>
              <div className="flex items-center gap-3">
                <button
                  onClick={toggleMute}
                  className="text-gray-400 hover:text-brand transition-colors"
                >
                  {isMuted || volume === 0 ? <VolumeX size={16} /> : <Volume2 size={16} />}
                </button>
                <input
                  type="range"
                  min="0"
                  max="1"
                  step="0.01"
                  value={volume}
                  onChange={handleVolumeChange}
                  className="flex-1 h-1 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-brand"
                />
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      <motion.div
        layout
        initial={false}
        animate={{
          width: isMinimized ? 56 : 'auto',
          borderRadius: isMinimized ? 28 : 32
        }}
        transition={{ type: 'spring', damping: 20, stiffness: 200 }}
        className="pointer-events-auto group relative flex items-center bg-brand hover:bg-brand-hover text-white shadow-xl transition-colors duration-300 overflow-hidden cursor-pointer"
        onClick={() => {
          if (isMinimized) {
            setIsMinimized(false);
          }
        }}
      >
        <div
          onClick={(e) => {
            if (isMinimized) {
              // If minimized, don't stop propagation so the parent can expand it
              // But still toggle play
              togglePlay();
            } else {
              e.stopPropagation();
              togglePlay();
            }
          }}
          className="flex-shrink-0 flex items-center justify-center w-14 h-14 rounded-full bg-transparent hover:bg-white/10 transition-colors"
        >
          {isLoading ? (
            <motion.div
              animate={{ rotate: 360 }}
              transition={{ repeat: Infinity, duration: 1, ease: "linear" }}
              className="w-5 h-5 border-2 border-white/30 border-t-white rounded-full"
            />
          ) : isPlaying ? (
            <Pause size={22} fill="currentColor" />
          ) : (
            <Play size={22} fill="currentColor" className="ml-1" />
          )}
        </div>

        <AnimatePresence>
          {!isMinimized && (
            <motion.div
              initial={{ opacity: 0, width: 0 }}
              animate={{ opacity: 1, width: 'auto' }}
              exit={{ opacity: 0, width: 0 }}
              className="flex items-center gap-3 pr-4 whitespace-nowrap"
            >
              <div
                className="flex flex-col items-start cursor-pointer hover:opacity-80 transition-opacity"
                onClick={() => setIsMinimized(true)}
                title="Minimize to icon"
              >
                <span className="text-[10px] font-bold uppercase tracking-tighter opacity-70 leading-none mb-0.5 flex items-center gap-1">
                  Live Radio <Minimize2 size={10} className="opacity-50" />
                </span>
                <span className="text-xs font-semibold leading-none">{currentStation.name}</span>
              </div>

              <div
                onClick={(e) => {
                  e.stopPropagation();
                  resetAutoMinimizeTimer();
                  setIsExpanded(!isExpanded);
                }}
                className="border-l border-white/20 pl-2 text-white/50 hover:text-white transition-colors py-2"
              >
                {isExpanded ? <ChevronDown size={18} /> : <ChevronUp size={18} />}
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        {/* Playing Indicator Dots */}
        {isPlaying && (
          <div className="absolute top-1 right-1 flex gap-0.5">
            <span className="flex h-3 w-3 relative">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-green-400 opacity-75"></span>
              <span className="relative inline-flex rounded-full h-3 w-3 bg-green-500 border-2 border-brand"></span>
            </span>
          </div>
        )}
      </motion.div>
    </div>
  );
};
