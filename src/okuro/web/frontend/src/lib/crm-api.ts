/**
 * CRM API client — /api/crm/*.
 *
 * Relational person model (migration 056): companies, affiliations (hats),
 * connections (your edges), engagements (project × hat × company), and the
 * engagement_resolve preview. Separate from people-api.ts on purpose — it
 * maps to the isolated /api/crm router, so the working /api/people surface is
 * never touched.
 *
 * Write methods echo {message} plus the re-queried list, so callers can render
 * persisted state without a second round-trip.
 */

import { api } from "./api";

export type Company = {
  id: string;
  name: string;
  brand_id?: string | null;
  domain?: string | null;
  notes?: string | null;
  members?: number;
};

export type BrandOption = { id: string; name: string };
export type ProjectOption = { id: string; name: string };

export type Affiliation = {
  id: string;
  company_id: string;
  company_name: string;
  role?: string | null;
  role_class?: string | null;
  status: string;
  is_primary: number;
  started_at?: string | null;
  ended_at?: string | null;
};

export type Connection = {
  id: string;
  connection_type: string;
  context?: string | null;
  company_id?: string | null;
  company_name?: string | null;
  notes?: string | null;
};

export type Engagement = {
  id: string;
  project_slug: string;
  affiliation_id?: string | null;
  company_id?: string | null;
  company_name?: string | null;
  brand_id?: string | null;
  hat_role?: string | null;
  notes?: string | null;
};

export type AffiliationInput = {
  company_id: string;
  role?: string;
  role_class?: string;
  is_primary?: boolean;
  status?: string;
};

export type ConnectionInput = {
  connection_type: string;
  context?: string;
  company_id?: string;
  notes?: string;
};

export type EngagementInput = {
  project_slug: string;
  affiliation_id?: string;
  company_id?: string;
  notes?: string;
};

const enc = encodeURIComponent;

export const crmApi = {
  // companies
  listCompanies: () => api<{ companies: Company[] }>("/api/crm/companies"),
  createCompany: (body: {
    name: string;
    brand_id?: string;
    domain?: string;
    notes?: string;
    company_id?: string;
  }) =>
    api<{ message: string; companies: Company[] }>("/api/crm/companies", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  setCompanyBrand: (companyId: string, brandId: string) =>
    api<{ message: string }>(`/api/crm/companies/${enc(companyId)}/brand`, {
      method: "PUT",
      body: JSON.stringify({ brand_id: brandId }),
    }),
  listBrands: () => api<{ brands: BrandOption[] }>("/api/crm/brands"),
  listProjects: () => api<{ projects: ProjectOption[] }>("/api/crm/projects"),

  // affiliations (hats)
  listAffiliations: (personId: string) =>
    api<{ affiliations: Affiliation[] }>(
      `/api/crm/people/${enc(personId)}/affiliations`,
    ),
  addAffiliation: (personId: string, body: AffiliationInput) =>
    api<{ message: string; affiliations: Affiliation[] }>(
      `/api/crm/people/${enc(personId)}/affiliations`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  setPrimaryAffiliation: (personId: string, affiliationId: string) =>
    api<{ message: string; affiliations: Affiliation[] }>(
      `/api/crm/people/${enc(personId)}/affiliations/${enc(affiliationId)}/primary`,
      { method: "PUT" },
    ),
  removeAffiliation: (personId: string, affiliationId: string) =>
    api<{ message: string; affiliations: Affiliation[] }>(
      `/api/crm/people/${enc(personId)}/affiliations/${enc(affiliationId)}`,
      { method: "DELETE" },
    ),

  // connections (person → user)
  listConnections: (personId: string) =>
    api<{ connections: Connection[] }>(
      `/api/crm/people/${enc(personId)}/connections`,
    ),
  addConnection: (personId: string, body: ConnectionInput) =>
    api<{ message: string; connections: Connection[] }>(
      `/api/crm/people/${enc(personId)}/connections`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  removeConnection: (personId: string, connectionId: string) =>
    api<{ message: string; connections: Connection[] }>(
      `/api/crm/people/${enc(personId)}/connections/${enc(connectionId)}`,
      { method: "DELETE" },
    ),

  // engagements (project × hat × company)
  listEngagements: (personId: string) =>
    api<{ engagements: Engagement[] }>(
      `/api/crm/people/${enc(personId)}/engagements`,
    ),
  addEngagement: (personId: string, body: EngagementInput) =>
    api<{ message: string; engagements: Engagement[] }>(
      `/api/crm/people/${enc(personId)}/engagements`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  removeEngagement: (personId: string, engagementId: string) =>
    api<{ message: string; engagements: Engagement[] }>(
      `/api/crm/people/${enc(personId)}/engagements/${enc(engagementId)}`,
      { method: "DELETE" },
    ),

  // engagement_resolve preview
  resolve: (personId: string, project: string) =>
    api<{ person_id: string; project: string; markdown: string }>(
      `/api/crm/people/${enc(personId)}/resolve?project=${enc(project)}`,
    ),
};
