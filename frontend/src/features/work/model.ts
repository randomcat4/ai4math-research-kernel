export interface RouteChoice {routeId:string;label:string;thesis:string;state:"PROPOSED"|"APPROVED"|"STOPPED";priority:number;budget:Record<string,number>;stopReason?:string}
export interface AttemptView {attemptId:string;workerRunId:string;workerLabel:string;state:string;startedAt:string;publicSummary:string}
export interface WorkItemView {workItemId:string;stableLabel:string;title:string;state:string;routeId:string;position:number;attempts:AttemptView[]}
