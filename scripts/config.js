// ============================================================
// config.js — Configuration centrale du trafic synthétique
// C'est le SEUL fichier à modifier pour changer l'URL de base.
// ============================================================

// URL de base de l'Ingress Traefik.
// Exemples :
//   - Via /etc/hosts + port-forward k3d : http://university.local:8080
//   - Via IP Docker directe             : http://172.18.0.2
//   - Via localhost port-forward         : http://localhost:8080
export const BASE_URL = __ENV.BASE_URL || "http://university.local:8080";

// Endpoints des 3 services (via le reverse proxy Traefik/Nginx)
export const STUDENT_URL    = `${BASE_URL}/api/students`;
export const TEACHER_URL    = `${BASE_URL}/api/teachers`;
export const ENROLLMENT_URL = `${BASE_URL}/api/enrollments`;

// Timeouts globaux
export const TIMEOUT = "10s";
