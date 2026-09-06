import React, { createContext, useContext, useState, useEffect } from 'react';
import { User } from '../types';
import { currentUserMock } from '../services/mockData';
import { ApiService } from '../services/api';

interface AuthContextType {
  user: User | null;
  isAuthenticated: boolean;
  login: (email: string, pass: string) => Promise<void>;
  signup: (name: string, email: string, pass: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export const AuthProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [user, setUser] = useState<User | null>(currentUserMock);

  const login = async (email: string, pass: string) => {
    const loggedUser = await ApiService.login(email, pass);
    setUser(loggedUser);
  };

  const signup = async (name: string, email: string, pass: string) => {
    const newUser = await ApiService.signup(name, email, pass);
    setUser(newUser);
  };

  const logout = () => {
    ApiService.setToken(null);
    setUser(null);
  };

  return (
    <AuthContext.Provider value={{ user, isAuthenticated: !!user, login, signup, logout }}>
      {children}
    </AuthContext.Provider>
  );
};

export const useAuth = () => {
  const context = useContext(AuthContext);
  if (!context) throw new Error('useAuth must be used within AuthProvider');
  return context;
};
